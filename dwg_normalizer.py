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
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field

import ezdxf

# --------------------------------------------------------------------------
# ODA File Converter - ЗАПАСНОЙ путь чтения/записи DWG, если AutoCAD не найден.
# Без членства в ODA он разрешён только для некоммерческого использования.
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
# ОСНОВНОЙ путь чтения/записи DWG - AutoCAD Core Console (accoreconsole.exe).
# Она есть в любом AutoCAD начиная с 2013, работает без окна, лицензия -
# ваш же AutoCAD. DWG -> DXF командой DXFOUT, правка DXF через ezdxf,
# DXF -> DWG командой SAVEAS.
# --------------------------------------------------------------------------
ACAD_SEARCH_PATTERNS = [
    r"C:\Program Files\Autodesk\AutoCAD*\accoreconsole.exe",
]
ACAD_TIMEOUT = 15 * 60  # секунд на одну конвертацию

# Версия DWG -> ключевое слово формата для DXFOUT / SAVEAS.
# Сохраняем в той же версии, что была: файл откроется у всех, у кого открывался.
DWG_FORMATS = {
    "AC1015": "2000", "AC1018": "2004", "AC1021": "2007",
    "AC1024": "2010", "AC1027": "2013", "AC1032": "2018",
}

# Формат DWG менялся редко: AC1027 - это 2013..2017, AC1032 - с 2018 по 2027.
FORMAT_ORDER = ["2000", "2004", "2007", "2010", "2013", "2018"]


def acad_year(acad_path: str) -> int:
    """Год версии AutoCAD из имени папки: 'AutoCAD LT 2024' -> 2024, иначе 0."""
    years = re.findall(r"20\d\d", acad_path)   # ищем по всему пути: разделители разные
    return int(years[-1]) if years else 0


def acad_max_format(year: int) -> str:
    """Самый новый формат DWG, который открывает AutoCAD этого года."""
    for fmt in reversed(FORMAT_ORDER):
        if year >= int(fmt):
            return fmt
    return FORMAT_ORDER[0]


NO_ENGINE_MSG = (
    "Не найден AutoCAD. Для чтения и записи DWG нужен установленный AutoCAD 2013 "
    "или новее (используется его консольная версия accoreconsole.exe). Если AutoCAD "
    "стоит в нестандартной папке, укажи путь к accoreconsole.exe в профиле, ключ acad_path.")


def find_acad(explicit: str | None = None) -> str:
    """Путь к accoreconsole.exe: явный из профиля или самый свежий AutoCAD."""
    if explicit and os.path.isfile(explicit):
        return explicit
    found = []
    for pattern in ACAD_SEARCH_PATTERNS:
        found.extend(glob.glob(pattern))
    return max(found, key=_version_key) if found else ""


def resolve_engine(profile: dict | None = None) -> dict:
    """Чем открывать и сохранять DWG: AutoCAD, иначе ODA, иначе нечем."""
    profile = profile or {}
    acad = find_acad(profile.get("acad_path"))
    formats = {**DWG_FORMATS, **(profile.get("dwg_formats") or {})}
    if acad:
        name = os.path.basename(os.path.dirname(acad))
        return {"kind": "autocad", "path": acad, "label": f"{name} (Core Console)",
                "year": acad_year(acad), "formats": formats}
    oda = set_oda_path(profile.get("oda_path"))
    if oda:
        return {"kind": "oda", "path": oda, "label": "ODA File Converter",
                "year": 0, "formats": formats}
    return {"kind": None, "path": "", "label": "не найден", "year": 0, "formats": formats}


def dwg_version(path: str) -> str:
    """Версия DWG записана в первых 6 байтах файла: 'AC1032' - формат 2018."""
    with open(path, "rb") as f:
        return f.read(6).decode("ascii", errors="replace")


def _work_dir() -> str:
    """Временная папка, путь к которой состоит только из латиницы: скрипты
    AutoCAD (.scr) надёжно понимают только такие пути, а имя пользователя
    Windows бывает кириллицей."""
    base = tempfile.gettempdir()
    if not base.isascii():
        base = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "DWGNormalizer")
        os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="dwgn_", dir=base)


def _console_text(raw: bytes) -> str:
    """Вывод accoreconsole бывает в UTF-16, бывает в OEM-кодировке."""
    if raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16-le", errors="replace")
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _run_acad(acad: str, drawing: str, commands: list[str], work: str,
              result: str, what: str) -> None:
    """Открывает чертёж в accoreconsole, выполняет команды, проверяет результат."""
    script = os.path.join(work, "run.scr")
    with open(script, "w", encoding="ascii", newline="\r\n") as f:
        f.write("\n".join([*commands, "_.QUIT", "_Y"]) + "\n")
    try:
        proc = subprocess.run(
            [acad, "/i", drawing, "/s", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=ACAD_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # без чёрного окна
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"AutoCAD не уложился в {ACAD_TIMEOUT // 60} мин ({what})") from None
    if not os.path.isfile(result):
        lines = [ln for ln in _console_text(proc.stdout).splitlines() if ln.strip()]
        tail = "\n".join(lines[-12:])
        raise RuntimeError(f"AutoCAD не смог выполнить {what}. Последние строки консоли:\n{tail}")


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
    "acad_path": None,      # путь к accoreconsole.exe; None = найти автоматически
    "oda_path": None,       # запасной ODA File Converter; None = найти автоматически
    "dwg_formats": {},      # дополнения к таблице форматов DWG, если выйдет новый
    "text": {
        "font": "isocpeur.ttf",
        "font_family": "ISOCPEUR",  # имя семейства того же шрифта (как в AutoCAD)
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
    with open(path, "r", encoding="utf-8-sig") as f:  # -sig: Блокнот добавляет BOM
        user = json.load(f)
    for section in ("text", "dimensions"):
        if section in user:
            profile[section].update(user[section])
    for key in ("name", "acad_path", "oda_path", "dwg_formats"):
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
def check_format(version: str, engine: dict) -> str:
    """Формат чертежа -> ключевое слово для DXFOUT/SAVEAS.
    Заодно проверяет, что установленный AutoCAD такой формат открывает."""
    fmt = (engine.get("formats") or DWG_FORMATS).get(version)
    if not fmt:
        raise RuntimeError(
            f"Неизвестный формат DWG ({version}). Возможно, вышла новая версия AutoCAD. "
            f"Добавь пару \"{version}\": \"<год формата>\" в профиль, ключ dwg_formats.")
    year = engine.get("year") or 0
    if year:
        top = acad_max_format(year)
        if FORMAT_ORDER.index(fmt) > FORMAT_ORDER.index(top):
            raise RuntimeError(
                f"Чертёж сохранён в формате DWG {fmt}, а установленный AutoCAD {year} "
                f"открывает форматы до {top} включительно — он этот файл не откроет. "
                f"Обработай его на компьютере с AutoCAD {fmt} или новее, либо попроси "
                f"автора пересохранить чертёж в формате {top}.")
    return fmt


def open_drawing(path: str, engine: dict | None = None):
    """Открывает .dxf напрямую, .dwg - через AutoCAD (или ODA, если AutoCAD нет)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dxf":
        return ezdxf.readfile(path)
    if ext != ".dwg":
        raise ValueError(f"Неподдерживаемое расширение: {ext}")
    engine = engine or resolve_engine()
    if engine["kind"] == "oda":
        return odafc.readfile(path)
    if engine["kind"] != "autocad":
        raise RuntimeError(NO_ENGINE_MSG)
    work = _work_dir()
    try:
        src = os.path.join(work, "in.dwg")   # копия с латинским именем
        shutil.copyfile(path, src)
        fmt = check_format(dwg_version(src), engine)
        dxf = os.path.join(work, "in.dxf")
        _run_acad(engine["path"], src, ["_.DXFOUT", f'"{dxf}"', "_V", fmt, "16"],
                  work, dxf, "DWG -> DXF")
        return ezdxf.readfile(dxf)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def save_drawing(doc, path: str, engine: dict | None = None) -> None:
    """Сохраняет .dxf напрямую, .dwg - через AutoCAD (или ODA) в исходной версии."""
    ext = os.path.splitext(path)[1].lower()
    if ext != ".dwg":
        doc.saveas(path)
        return
    engine = engine or resolve_engine()
    if engine["kind"] == "oda":
        odafc.export_dwg(doc, path, replace=True)
        return
    if engine["kind"] != "autocad":
        raise RuntimeError(NO_ENGINE_MSG)
    work = _work_dir()
    try:
        dxf = os.path.join(work, "out.dxf")
        doc.saveas(dxf)
        dwg = os.path.join(work, "out.dwg")
        fmt = (engine.get("formats") or DWG_FORMATS).get(doc.dxfversion, "2018")
        _run_acad(engine["path"], dxf, ["_.SAVEAS", fmt, f'"{dwg}"'], work, dwg, "DXF -> DWG")
        try:
            shutil.copyfile(dwg, path)
        except PermissionError:
            raise RuntimeError(f"Не удалось записать {path}: файл открыт в AutoCAD? "
                               "Закрой его и запусти ещё раз.") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
        family, italic, bold = s.get_extended_font_data()
        extra = f", семейство='{family}'" if family else ""
        extra += " ЖИРНЫЙ" if bold else ""
        extra += " КУРСИВ" if italic else ""
        rep.add(f"  {s.dxf.name}: font='{font}'{extra}, width={width}, oblique={oblique}")

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
        for ent in container:
            # атрибуты висят на вставке блока, а не лежат в контейнере сами
            items = [ent] + (list(ent.attribs) if ent.dxftype() == "INSERT" else [])
            for e in items:
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

    override_keys = Counter()
    for container in iter_containers(doc):
        for e in container:
            if e.dxftype() == "DIMENSION" and e.has_xdata("ACAD"):
                override_keys.update(e.override().dimstyle_attribs.keys())
    if override_keys:
        rep.add()
        rep.add("=== Что переопределено у размеров ===")
        for key, cnt in override_keys.most_common(8):
            rep.add(f"  {key}: {cnt}")

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


# Кодировка и pitch&family для кириллицы - так их пишет сам AutoCAD (c204, p34)
DEFAULT_CHARSET = 204
DEFAULT_PITCH = 34


def retarget_font_code(code: str, family: str) -> str:
    r"""Переписывает inline-код шрифта на целевой шрифт.

    \fArial|b1|i1|c204|p34;  ->  \fISOCPEUR|b0|i0|c204|p34;
    Кодировку (c) и pitch (p) сохраняет, жирность и курсив снимает.
    """
    params = [p for p in code[2:-1].split("|")[1:] if p[:1] in ("c", "p")]
    if not params:  # у \F (SHX) и коротких кодов параметров нет
        params = [f"c{DEFAULT_CHARSET}", f"p{DEFAULT_PITCH}"]
    return "\\f" + "|".join([family, "b0", "i0", *params]) + ";"


def sync_style_family(style, family: str | None) -> bool:
    """TTF-стиль хранит шрифт дважды: имя файла и семейство в XDATA (там же
    флаги жирный/курсив и кодировка). Семейство приводим к целевому, жирный
    и курсив снимаем, кодировку сохраняем. True, если что-то изменилось."""
    if not family or not style.has_xdata("ACAD"):
        return False
    old = [(tag.code, tag.value) for tag in style.get_xdata("ACAD")]
    new, got_family, got_flags = [], False, False
    for code, value in old:
        if code == 1000 and not got_family:
            value, got_family = family, True
        elif code == 1071 and not got_flags:
            value, got_flags = int(value) & ~(style.ITALIC | style.BOLD), True
        new.append((code, value))
    if new == old:
        return False
    style.set_xdata("ACAD", new)
    return True


def normalize(doc, profile: dict, rep: Report | None = None) -> Report:
    rep = rep or Report()
    tcfg = profile["text"]
    dcfg = profile["dimensions"]

    rep.add(f"Профиль: {profile.get('name', 'без имени')}")
    rep.add()

    # ---- 1. шрифт во всех текстовых стилях ----
    target_font = tcfg.get("font")
    target_family = tcfg.get("font_family")
    # цель бывает и SHX (ГОСТ-шрифт), тогда правила другие
    target_is_shx = bool(target_font) and (
        target_font.lower().endswith(".shx") or "." not in target_font)
    skip = {n.lower() for n in (tcfg.get("skip_styles") or [])}
    # Стили, которые после этого шага сами дают целевой шрифт.
    # Только у надписей на них inline-код шрифта можно просто вырезать.
    target_styles: set[str] = set()
    if target_font:
        rep.add(f"--- Текстовые стили -> '{target_font}' ---")
        for s in doc.styles:
            name = s.dxf.name
            if name.lower() in skip:
                rep.add(f"  [пропуск] {name}")
                continue
            old_font = s.dxf.get("font", "")
            # SHX-шрифты по умолчанию не трогаем: замена на TTF
            # может сломать спецсимволы (диаметр, градус и т.п.)
            is_shx = old_font.lower().endswith(".shx") or "." not in old_font
            # SHX не трогаем только при переводе на TTF: там рискуют спецсимволы
            if is_shx and not tcfg.get("convert_shx", False) and not target_is_shx:
                rep.add(f"  [SHX, пропуск] {name}: '{old_font}'")
                continue
            if old_font.lower() != target_font.lower():
                s.dxf.font = target_font
                rep.add(f"  {name}: '{old_font}' -> '{target_font}'")
                rep.count("styles_font")
            if target_is_shx:
                # у SHX-стиля не должно оставаться данных TTF-шрифта
                if s.has_extended_font_data:
                    s.discard_extended_font_data()
                    rep.add(f"  {name}: снято семейство TTF-шрифта")
                    rep.count("styles_family")
            else:
                if is_shx:
                    # был SHX, стал TTF: big font больше не нужен,
                    # семейство прописываем так же, как это делает AutoCAD
                    if s.dxf.hasattr("bigfont"):
                        s.dxf.discard("bigfont")
                    if target_family and not s.has_xdata("ACAD"):
                        s.set_xdata("ACAD", [(1000, target_family),
                                             (1071, (DEFAULT_CHARSET << 8) | DEFAULT_PITCH)])
                if sync_style_family(s, target_family):
                    rep.add(f"  {name}: семейство/жирный/курсив -> '{target_family}', обычный")
                    rep.count("styles_family")
            if tcfg.get("width") is not None:
                s.dxf.width = float(tcfg["width"])
                rep.count("styles_width")
            if tcfg.get("oblique") is not None:
                s.dxf.oblique = float(tcfg["oblique"])
                rep.count("styles_oblique")
            target_styles.add(name.lower())
        rep.add(f"  сменён файл шрифта: {rep.changes['styles_font']}, "
                f"исправлено семейство: {rep.changes['styles_family']}")

    # ---- 2. inline-коды в MTEXT ----
    rep.add()
    rep.add("--- Inline-коды в MTEXT ---")
    # Вырезать \f можно, только если стиль надписи сам даёт целевой шрифт.
    # Иначе надпись упадёт на шрифт стиля (например, на SHX, который мы
    # не трогаем), поэтому там код не вырезаем, а переписываем на целевой.
    keep_font_cfg = {**tcfg, "strip_inline_font": False}
    retarget = tcfg.get("strip_inline_font", True) and bool(target_family) and not target_is_shx
    for container in iter_containers(doc, include_blocks=tcfg.get("process_blocks", True)):
        where = getattr(container, "name", "modelspace")
        for e in container:
            if e.dxftype() != "MTEXT":
                continue
            old = e.text
            if e.dxf.get("style", "Standard").lower() in target_styles:
                new = clean_mtext(old, tcfg)
            else:
                new = clean_mtext(old, keep_font_cfg)
                if retarget:
                    new = RE_FONT.sub(
                        lambda m: retarget_font_code(m.group(0), target_family), new)
                elif RE_FONT.search(old):
                    rep.count("mtext_font_left")
            if new != old:
                e.text = new
                rep.count("mtext")
                if rep.changes["mtext"] <= 10:  # показываем первые 10 примеров
                    rep.add(f"  [{where}] {old[:70]!r}")
                    rep.add(f"        -> {new[:70]!r}")
    rep.add(f"  изменено MTEXT: {rep.changes['mtext']}")
    if rep.changes["mtext_font_left"]:
        rep.add(f"  оставлено inline-шрифтов (стиль даёт другой шрифт): "
                f"{rep.changes['mtext_font_left']}")

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
        engine = None
        if src.lower().endswith(".dwg"):
            engine = resolve_engine(profile)
            if not engine["kind"]:
                raise RuntimeError(NO_ENGINE_MSG)
            rep.add(f"Чтение/запись DWG: {engine['label']}")
            rep.add()
        doc = open_drawing(src, engine)

        if scan_only:
            scan(doc, rep)
            return rep

        scan(doc, rep)
        rep.add()
        rep.add("=" * 50)
        rep.add()
        normalize(doc, profile, rep)

        dst = dst or output_path(src)
        save_drawing(doc, dst, engine)
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
