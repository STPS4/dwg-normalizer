"""
DWG Normalizer - приведение шрифтов и размерных стилей к единому стандарту.

Ядро без интерфейса. Используется из gui.py или напрямую из командной строки:
    python dwg_normalizer.py чертеж.dwg
    python dwg_normalizer.py чертеж.dwg --profile gost.json
    python dwg_normalizer.py чертеж.dwg --scan          (только диагностика)
    python dwg_normalizer.py папка\\ --batch            (вся папка)
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field

import ezdxf

# --------------------------------------------------------------------------
# ODA File Converter.
# Папка установки содержит номер версии ("ODAFileConverter 27.1.0"), поэтому
# на каждом ПК путь свой - ищем exe сами. Явный путь из профиля (oda_path)
# имеет приоритет. ВАЖНО: путь задаётся через ezdxf.options.set(...),
# присваивание атрибута модулю odafc не работает.
# --------------------------------------------------------------------------
ODA_SEARCH_PATTERNS = [
    r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
    r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
]


def _version_key(exe_path: str) -> list[int]:
    """'ODAFileConverter 27.1.0' -> [27, 1, 0] для сортировки по версии."""
    folder = os.path.basename(os.path.dirname(exe_path))
    return [int(n) for n in re.findall(r"\d+", folder)]


def find_oda() -> str:
    """Находит установленный ODAFileConverter.exe (самую свежую версию)."""
    found = []
    for pattern in ODA_SEARCH_PATTERNS:
        found.extend(glob.glob(pattern))
    return max(found, key=_version_key) if found else ""


def set_oda_path(path: str | None = None) -> str:
    """Сообщает ezdxf, где лежит ODA File Converter. Возвращает итоговый путь."""
    if not path or not os.path.isfile(path):
        path = find_oda()
    if path:
        ezdxf.options.set("odafc-addon", "win_exec_path", path)
    return path


set_oda_path()
from ezdxf.addons import odafc  # noqa: E402


# --------------------------------------------------------------------------
# Регулярные выражения для inline-кодов внутри MTEXT
# --------------------------------------------------------------------------
# \fArial|b1|i1|c204|p34;  - смена шрифта + жирность + курсив
RE_FONT = re.compile(r"\\[fF][^;]*;")
# \H2.5x;  или  \H0.7;    - переопределение высоты
RE_HEIGHT = re.compile(r"\\H[\d.]+x?;")
# \W0.85;                  - переопределение ширины
RE_WIDTH = re.compile(r"\\W[\d.]+;")
# \Q15;                    - наклон (курсив через oblique)
RE_OBLIQUE = re.compile(r"\\Q-?[\d.]+;")

# Блоки, которые нельзя трогать: пространства и отрисованная геометрия размеров
SPECIAL_BLOCK_PREFIX = "*"


# --------------------------------------------------------------------------
# Профиль стандарта
# --------------------------------------------------------------------------
DEFAULT_PROFILE = {
    "name": "ГОСТ 2.304 (прямой)",
    "oda_path": None,       # None = найти автоматически
    "text": {
        "font": "isocpeur.ttf",
        "width": None,          # None = не трогать. Например 0.85
        "oblique": None,        # None = не трогать. Например 15.0
        "skip_styles": [],      # стили-исключения, например ["Standard"]
        "convert_shx": False,   # True = переводить и SHX-стили на TTF (рискованно)
        "strip_inline_font": True,
        "strip_inline_height": False,
        "strip_inline_width": False,
        "strip_inline_oblique": True,
        "process_blocks": True,
    },
    "dimensions": {
        "enabled": True,
        "target_style": None,   # None = взять самый используемый в чертеже
        "clear_overrides": True,
        "text_style": None,     # None = не менять текстовый стиль размеров
    },
}


def load_profile(path: str | None) -> dict:
    """Читает профиль из JSON. Недостающие ключи берутся из DEFAULT_PROFILE."""
    profile = json.loads(json.dumps(DEFAULT_PROFILE))  # глубокая копия
    if not path:
        return profile
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    for section in ("text", "dimensions"):
        if section in user:
            profile[section].update(user[section])
    for key in ("name", "oda_path"):
        if key in user:
            profile[key] = user[key]
    return profile


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------
@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    changes: Counter = field(default_factory=Counter)
    ok: bool = True
    error: str = ""

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def count(self, key: str, n: int = 1) -> None:
        self.changes[key] += n

    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------
# Открытие / сохранение
# --------------------------------------------------------------------------
def open_drawing(path: str):
    """Открывает .dwg (через ODA) или .dxf (напрямую)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dwg":
        return odafc.readfile(path)
    if ext == ".dxf":
        return ezdxf.readfile(path)
    raise ValueError(f"Неподдерживаемое расширение: {ext}")


def save_drawing(doc, path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dwg":
        odafc.export_dwg(doc, path, replace=True)
    else:
        doc.saveas(path)


def output_path(src: str, suffix: str = "_normalized") -> str:
    base, ext = os.path.splitext(src)
    return f"{base}{suffix}{ext}"


# --------------------------------------------------------------------------
# Обход всех контейнеров с текстом
# --------------------------------------------------------------------------
def iter_containers(doc, include_blocks: bool = True):
    """Модель + листы + именованные блоки (без *Model_Space, *D1 и т.п.)."""
    yield doc.modelspace()
    for layout in doc.layouts:
        if layout.name.lower() != "model":
            yield layout
    if include_blocks:
        for block in doc.blocks:
            if not block.name.startswith(SPECIAL_BLOCK_PREFIX):
                yield block


# --------------------------------------------------------------------------
# ДИАГНОСТИКА
# --------------------------------------------------------------------------
def scan(doc, rep: Report | None = None) -> Report:
    rep = rep or Report()

    rep.add(f"Версия формата: {doc.dxfversion} ({doc.acad_release})")
    rep.add()

    # --- текстовые стили ---
    rep.add(f"=== Текстовые стили: {len(doc.styles)} ===")
    for s in doc.styles:
        font = s.dxf.get("font", "")
        width = s.dxf.get("width", 1.0)
        oblique = s.dxf.get("oblique", 0.0)
        rep.add(f"  {s.dxf.name}: font='{font}', width={width}, oblique={oblique}")

    # --- размерные стили ---
    rep.add()
    rep.add(f"=== Размерные стили: {len(doc.dimstyles)} ===")
    for d in doc.dimstyles:
        rep.add(f"  {d.dxf.name}: dimtxt={d.dxf.get('dimtxt')}, "
                f"dimasz={d.dxf.get('dimasz')}, dimtxsty='{d.dxf.get('dimtxsty')}'")

    # --- что реально используется ---
    used_text = Counter()
    inline_font = inline_bold = inline_italic = 0
    n_text = n_mtext = 0

    for container in iter_containers(doc):
        for e in container:
            dxftype = e.dxftype()
            if dxftype in ("TEXT", "ATTDEF", "ATTRIB"):
                used_text[e.dxf.get("style", "Standard")] += 1
                n_text += 1
            elif dxftype == "MTEXT":
                used_text[e.dxf.get("style", "Standard")] += 1
                n_mtext += 1
                raw = e.text
                if RE_FONT.search(raw):
                    inline_font += 1
                if "|b1" in raw:
                    inline_bold += 1
                if "|i1" in raw:
                    inline_italic += 1

    used_dim = Counter()
    n_override = 0
    for container in iter_containers(doc):
        for e in container:
            if e.dxftype() == "DIMENSION":
                used_dim[e.dxf.get("dimstyle", "Standard")] += 1
                if e.has_xdata("ACAD"):
                    n_override += 1

    rep.add()
    rep.add("=== Найдено объектов ===")
    rep.add(f"  TEXT/ATTRIB: {n_text}")
    rep.add(f"  MTEXT:       {n_mtext}")
    rep.add(f"  DIMENSION:   {sum(used_dim.values())}")

    rep.add()
    rep.add("=== Текстовые стили в работе ===")
    for name, cnt in used_text.most_common():
        rep.add(f"  {name}: {cnt}")

    declared = {s.dxf.name for s in doc.styles}
    unused = sorted(declared - set(used_text))
    if unused:
        rep.add()
        rep.add(f"Не используются ({len(unused)}): {', '.join(unused)}")

    rep.add()
    rep.add("=== Размерные стили в работе ===")
    for name, cnt in used_dim.most_common():
        rep.add(f"  {name}: {cnt}")

    rep.add()
    rep.add("=== Проблемы ===")
    rep.add(f"  MTEXT с inline-шрифтом:  {inline_font}")
    rep.add(f"  из них жирных (|b1):     {inline_bold}")
    rep.add(f"  из них курсивных (|i1):  {inline_italic}")
    rep.add(f"  размеров с оверрайдами:  {n_override}")

    return rep


def dominant_dimstyle(doc) -> str | None:
    """Самый часто используемый размерный стиль - разумный кандидат в эталон."""
    used = Counter()
    for container in iter_containers(doc):
        for e in container:
            if e.dxftype() == "DIMENSION":
                used[e.dxf.get("dimstyle", "Standard")] += 1
    return used.most_common(1)[0][0] if used else None


# --------------------------------------------------------------------------
# НОРМАЛИЗАЦИЯ
# --------------------------------------------------------------------------
def clean_mtext(raw: str, cfg: dict) -> str:
    """Вырезает inline-переопределения согласно профилю."""
    out = raw
    if cfg.get("strip_inline_font", True):
        out = RE_FONT.sub("", out)
    if cfg.get("strip_inline_height", False):
        out = RE_HEIGHT.sub("", out)
    if cfg.get("strip_inline_width", False):
        out = RE_WIDTH.sub("", out)
    if cfg.get("strip_inline_oblique", True):
        out = RE_OBLIQUE.sub("", out)
    return out


def normalize(doc, profile: dict, rep: Report | None = None) -> Report:
    rep = rep or Report()
    tcfg = profile["text"]
    dcfg = profile["dimensions"]

    rep.add(f"Профиль: {profile.get('name', 'без имени')}")
    rep.add()

    # ---- 1. шрифт во всех текстовых стилях ----
    target_font = tcfg.get("font")
    skip = set(tcfg.get("skip_styles") or [])
    if target_font:
        rep.add(f"--- Текстовые стили -> '{target_font}' ---")
        for s in doc.styles:
            name = s.dxf.name
            if name in skip:
                rep.add(f"  [пропуск] {name}")
                continue
            old_font = s.dxf.get("font", "")
            # SHX-шрифты по умолчанию не трогаем: замена на TTF
            # может сломать спецсимволы (диаметр, градус и т.п.)
            is_shx = old_font.lower().endswith(".shx") or "." not in old_font
            if is_shx and not tcfg.get("convert_shx", False):
                rep.add(f"  [SHX, пропуск] {name}: '{old_font}'")
                continue
            if old_font != target_font:
                s.dxf.font = target_font
                rep.add(f"  {name}: '{old_font}' -> '{target_font}'")
                rep.count("styles_font")
            if tcfg.get("width") is not None:
                s.dxf.width = float(tcfg["width"])
                rep.count("styles_width")
            if tcfg.get("oblique") is not None:
                s.dxf.oblique = float(tcfg["oblique"])
                rep.count("styles_oblique")
        rep.add(f"  изменено стилей: {rep.changes['styles_font']}")

    # ---- 2. inline-коды в MTEXT ----
    rep.add()
    rep.add("--- Очистка inline-кодов в MTEXT ---")
    for container in iter_containers(doc, include_blocks=tcfg.get("process_blocks", True)):
        where = getattr(container, "name", "modelspace")
        for e in container:
            if e.dxftype() != "MTEXT":
                continue
            old = e.text
            new = clean_mtext(old, tcfg)
            if new != old:
                e.text = new
                rep.count("mtext")
                if rep.changes["mtext"] <= 10:  # показываем первые 10 примеров
                    rep.add(f"  [{where}] {old[:70]!r}")
                    rep.add(f"        -> {new[:70]!r}")
    rep.add(f"  изменено MTEXT: {rep.changes['mtext']}")

    # ---- 3. размеры ----
    if dcfg.get("enabled", True):
        target = dcfg.get("target_style") or dominant_dimstyle(doc)
        rep.add()
        rep.add("--- Размеры ---")
        if not target:
            rep.add("  размеров в чертеже нет")
        elif target not in doc.dimstyles:
            rep.add(f"  ! стиль '{target}' отсутствует в чертеже, размеры не тронуты")
            rep.ok = False
        else:
            rep.add(f"  эталонный стиль: '{target}'")

            # текстовый стиль для размерных подписей
            if dcfg.get("text_style"):
                ts = dcfg["text_style"]
                if ts in doc.styles:
                    doc.dimstyles.get(target).dxf.dimtxsty = ts
                    rep.add(f"  подписи размеров -> стиль '{ts}'")
                else:
                    rep.add(f"  ! текстовый стиль '{ts}' не найден, пропущен")

            for container in iter_containers(doc):
                for e in container:
                    if e.dxftype() != "DIMENSION":
                        continue
                    if e.dxf.get("dimstyle") != target:
                        e.dxf.dimstyle = target
                        rep.count("dim_restyled")
                    if dcfg.get("clear_overrides", True) and e.has_xdata("ACAD"):
                        e.discard_xdata("ACAD")
                        rep.count("dim_overrides_cleared")

            rep.add(f"  переназначено на эталон: {rep.changes['dim_restyled']}")
            rep.add(f"  снято переопределений:   {rep.changes['dim_overrides_cleared']}")

    rep.add()
    rep.add("=== Итого ===")
    total = sum(rep.changes.values())
    for k, v in rep.changes.items():
        rep.add(f"  {k}: {v}")
    rep.add(f"  всего правок: {total}")

    return rep


# --------------------------------------------------------------------------
# Обработка одного файла целиком
# --------------------------------------------------------------------------
def process_file(src: str, profile: dict, dst: str | None = None,
                 scan_only: bool = False) -> Report:
    rep = Report()
    rep.add(f"### {os.path.basename(src)}")
    rep.add()
    try:
        if src.lower().endswith(".dwg"):
            oda = set_oda_path(profile.get("oda_path"))
            if not oda:
                raise RuntimeError(
                    "Не найден ODA File Converter. Установи его "
                    "(https://www.opendesign.com/guestfiles/oda_file_converter) "
                    "или укажи путь к ODAFileConverter.exe в профиле, ключ oda_path")
            rep.add(f"ODA File Converter: {oda}")
            rep.add()
        doc = open_drawing(src)

        if scan_only:
            scan(doc, rep)
            return rep

        scan(doc, rep)
        rep.add()
        rep.add("=" * 50)
        rep.add()
        normalize(doc, profile, rep)

        dst = dst or output_path(src)
        save_drawing(doc, dst)
        rep.add()
        rep.add(f"✓ Сохранено: {dst}")

    except Exception as exc:  # noqa: BLE001
        rep.ok = False
        rep.error = str(exc)
        rep.add()
        rep.add(f"✗ ОШИБКА: {exc}")
    return rep


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Нормализация шрифтов и размеров в DWG/DXF")
    ap.add_argument("path", help="файл .dwg/.dxf или папка (с --batch)")
    ap.add_argument("--profile", "-p", help="JSON-профиль стандарта")
    ap.add_argument("--scan", "-s", action="store_true", help="только диагностика")
    ap.add_argument("--batch", "-b", action="store_true", help="обработать всю папку")
    ap.add_argument("--out", "-o", help="путь результата (для одного файла)")
    args = ap.parse_args()

    profile = load_profile(args.profile)

    if args.batch:
        files = [os.path.join(args.path, f) for f in sorted(os.listdir(args.path))
                 if f.lower().endswith((".dwg", ".dxf"))
                 and "_normalized" not in f.lower()]
        if not files:
            print("В папке нет .dwg/.dxf файлов")
            return
        print(f"Найдено файлов: {len(files)}\n")
        for f in files:
            rep = process_file(f, profile, scan_only=args.scan)
            print(rep.text())
            print("\n" + "-" * 60 + "\n")
    else:
        rep = process_file(args.path, profile, dst=args.out, scan_only=args.scan)
        print(rep.text())


if __name__ == "__main__":
    main()
