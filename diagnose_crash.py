"""
Поиск причины, по которой AutoCAD падает при сохранении нормализованного чертежа.
Оригинал не меняется: всё делается с копиями во временной папке.

Запуск (из папки проекта):
    .venv\\Scripts\\python.exe diagnose_crash.py "чертеж.dwg"
    .venv\\Scripts\\python.exe diagnose_crash.py "чертеж.dwg" -p profile_gost.json

Что делает:
  1. Рассказывает, что в чертеже необычного: объекты сторонних приложений
     (AutoCAD Architecture, СПДС и т.п.), аннотативные размеры.
  2. Прогоняет одни и те же этапы по отдельности и смотрит, на каком AutoCAD падает:
       A. DXF от самого AutoCAD -> DWG, без нашей программы вообще;
       B. DXF, прочитанный и сохранённый ezdxf без единой правки;
       C. каждая правка нормализатора отдельно;
       D. все правки вместе, как в программе.
Отчёт пишется рядом с чертежом: имя_crash_report.txt
"""

import argparse
import copy
import os
import shutil
import sys
import time
from collections import Counter

import ezdxf
from ezdxf.entities.dxfentity import DXFTagStorage

import dwg_normalizer as core

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

REPORT: list[str] = []


def out(text: str = "") -> None:
    print(text, flush=True)
    REPORT.append(text)


# ---------------------------------------------------------------- сырой разбор DXF
def count_structure(path: str) -> Counter:
    """Считает объекты по секциям прямо по тексту DXF, без ezdxf.
    Нужно, чтобы проверить: не потерял ли ezdxf что-нибудь при пересохранении."""
    counts: Counter = Counter()
    section = None
    expect_name = False
    with open(path, "r", encoding="latin-1") as f:
        while True:
            code = f.readline()
            value = f.readline()
            if not value:
                break
            code, value = code.strip(), value.rstrip("\r\n").strip()
            if code == "0":
                if value == "SECTION":
                    expect_name = True
                elif value == "ENDSEC":
                    section = None
                elif value != "EOF" and section:
                    counts[(section, value)] += 1
            elif code == "2" and expect_name:
                section, expect_name = value, False
    return counts


# ---------------------------------------------------------------- что в файле
def describe(doc) -> None:
    out("=== Что в чертеже необычного ===")

    apps = Counter()
    for c in doc.classes:
        apps[c.dxf.get("app_name", "") or "(без имени)"] += 1
    out("Классы по приложениям (стандартные AutoCAD - это ObjectDBX Classes):")
    for app, n in apps.most_common():
        out(f"  {app}: {n}")

    foreign = Counter()
    for e in doc.entitydb.values():
        if isinstance(e, DXFTagStorage) or e.dxftype().startswith("ACAD_PROXY"):
            foreign[e.dxftype()] += 1
    out()
    out(f"Объекты, которые ezdxf не знает (хранит как есть): {sum(foreign.values())}")
    for t, n in foreign.most_common(15):
        out(f"  {t}: {n}")

    annot = Counter()
    for container in core.iter_containers(doc):
        for e in container:
            if e.dxftype() == "DIMENSION" and e.has_xdata("AcadAnnotative"):
                annot[e.dxf.get("dimstyle", "?")] += 1
    out()
    out(f"Аннотативных размеров: {sum(annot.values())}"
        + (f" (стили: {dict(annot)})" if annot else ""))


# ---------------------------------------------------------------- этапы правок
def step_none(doc, prof):
    pass


def step_styles(doc, prof):
    p = copy.deepcopy(prof)
    for k in ("strip_inline_font", "strip_inline_height",
              "strip_inline_width", "strip_inline_oblique"):
        p["text"][k] = False
    p["dimensions"]["enabled"] = False
    core.normalize(doc, p)


def step_mtext(doc, prof):
    tcfg = prof["text"]
    for container in core.iter_containers(doc, include_blocks=tcfg.get("process_blocks", True)):
        for e in container:
            if e.dxftype() == "MTEXT":
                new = core.clean_mtext(e.text, tcfg)
                if new != e.text:
                    e.text = new


def _restyle(doc, prof, skip_annotative: bool):
    target = prof["dimensions"].get("target_style") or core.dominant_dimstyle(doc)
    if not target or target not in doc.dimstyles:
        return
    for container in core.iter_containers(doc):
        for e in container:
            if e.dxftype() != "DIMENSION" or e.dxf.get("dimstyle") == target:
                continue
            if skip_annotative and e.has_xdata("AcadAnnotative"):
                continue
            e.dxf.dimstyle = target


def step_dim_restyle(doc, prof):
    _restyle(doc, prof, skip_annotative=False)


def step_dim_restyle_plain(doc, prof):
    _restyle(doc, prof, skip_annotative=True)


def step_dim_overrides(doc, prof):
    for container in core.iter_containers(doc):
        for e in container:
            if e.dxftype() == "DIMENSION" and e.has_xdata("ACAD"):
                e.discard_xdata("ACAD")


def step_all(doc, prof):
    core.normalize(doc, prof)


STEPS = [
    ("B", "ezdxf прочитал и сохранил, без правок", step_none),
    ("C1", "только текстовые стили", step_styles),
    ("C2", "только inline-коды MTEXT", step_mtext),
    ("C3", "только переназначение размерных стилей", step_dim_restyle),
    ("C3a", "то же, но аннотативные размеры не трогаем", step_dim_restyle_plain),
    ("C4", "только снятие переопределений размеров", step_dim_overrides),
    ("D", "все правки вместе (как в программе)", step_all),
]


# ---------------------------------------------------------------- запуск
def to_dwg(engine, dxf: str, dwg: str, fmt: str, work: str) -> tuple[str, str]:
    """DXF -> DWG через accoreconsole. Возвращает (итог, подробности)."""
    t0 = time.time()
    try:
        core._run_acad(engine["path"], dxf, ["_.SAVEAS", fmt, f'"{dwg}"'],
                       work, dwg, "DXF -> DWG")
        return "OK", f"{time.time() - t0:.0f} с"
    except RuntimeError as exc:
        text = str(exc)
        verdict = "ПАДЕНИЕ" if ("ФАТАЛЬНАЯ" in text or "FATAL" in text.upper()
                                or "Аварийное" in text) else "ОШИБКА"
        return verdict, text


def main() -> None:
    ap = argparse.ArgumentParser(description="Поиск причины падения AutoCAD")
    ap.add_argument("path", help="чертёж .dwg")
    ap.add_argument("-p", "--profile", default=os.path.join(APP_DIR, "profile_gost.json"))
    ap.add_argument("--keep", action="store_true", help="не удалять временную папку")
    args = ap.parse_args()

    if not os.path.isfile(args.path):
        print(f"Файл не найден: {args.path}")
        return
    profile = core.load_profile(args.profile if os.path.isfile(args.profile) else None)
    engine = core.resolve_engine(profile)
    if engine["kind"] != "autocad":
        print("Для диагностики нужен установленный AutoCAD.")
        return

    out(f"Файл: {args.path}")
    out(f"AutoCAD: {engine['label']}")
    out(f"Профиль: {profile.get('name')}")
    out()

    work = core._work_dir()
    results = []
    try:
        src = os.path.join(work, "in.dwg")
        shutil.copyfile(args.path, src)
        fmt = core.check_format(core.dwg_version(src), engine)
        base = os.path.join(work, "base.dxf")
        out(f"[1/{len(STEPS) + 2}] DWG -> DXF силами AutoCAD (формат {fmt})...")
        core._run_acad(engine["path"], src, ["_.DXFOUT", f'"{base}"', "_V", fmt, "16"],
                       work, base, "DWG -> DXF")
        out()

        doc = ezdxf.readfile(base)
        describe(doc)
        out()

        out(f"[2/{len(STEPS) + 2}] A: DXF от AutoCAD -> DWG, без ezdxf...")
        verdict, info = to_dwg(engine, base, os.path.join(work, "A.dwg"), fmt, work)
        results.append(("A", "DXF от самого AutoCAD, без нашей программы", verdict, info))

        base_counts = count_structure(base)
        for i, (key, title, func) in enumerate(STEPS, start=3):
            out(f"[{i}/{len(STEPS) + 2}] {key}: {title}...")
            d = ezdxf.readfile(base)
            func(d, profile)
            dxf = os.path.join(work, f"{key}.dxf")
            d.saveas(dxf)
            if key == "B":
                after = count_structure(dxf)
                lost = {k: base_counts[k] - after.get(k, 0)
                        for k in base_counts if base_counts[k] > after.get(k, 0)}
                added = {k: after[k] - base_counts.get(k, 0)
                         for k in after if after[k] > base_counts.get(k, 0)}
                if lost or added:
                    out("  ! ezdxf при пересохранении изменил состав файла:")
                    for (sec, t), n in sorted(lost.items()):
                        out(f"    пропало: {sec}/{t}: {n}")
                    for (sec, t), n in sorted(added.items()):
                        out(f"    добавилось: {sec}/{t}: {n}")
                else:
                    out("  состав объектов после ezdxf тот же, что у AutoCAD")
            verdict, info = to_dwg(engine, dxf, os.path.join(work, f"{key}.dwg"), fmt, work)
            results.append((key, title, verdict, info))

    except Exception as exc:  # noqa: BLE001
        out()
        out(f"✗ Диагностика прервалась: {exc}")
    finally:
        if args.keep:
            out(f"\nВременные файлы оставлены: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    out()
    out("=== Итог: на каком этапе AutoCAD не может сохранить DWG ===")
    for key, title, verdict, info in results:
        lines = [ln for ln in info.splitlines() if ln.strip("* ")]
        key_lines = [ln for ln in lines if "ОШИБКА" in ln.upper() or "ERROR" in ln.upper()]
        brief = info if verdict == "OK" else (key_lines or lines or [""])[-1].strip()[:90]
        out(f"  {key:3} {verdict:8} {title}  ({brief})")
    failed = [r for r in results if r[2] != "OK"]
    if failed:
        out()
        out("Подробности первого сбоя:")
        out(failed[0][3])

    report = os.path.splitext(args.path)[0] + "_crash_report.txt"
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT) + "\n")
    print(f"\nОтчёт сохранён: {report}")


if __name__ == "__main__":
    main()
