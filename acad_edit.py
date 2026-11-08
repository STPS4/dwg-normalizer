"""
Внесение правок прямо в DWG силами AutoCAD (accoreconsole + AutoLISP).

Зачем: пересборка чертежа через DXF ломается на файлах с объектами сторонних
приложений (AutoCAD Architecture, СПДС GraphiCS): AutoCAD падает, открывая
такой DXF, даже если его выгрузил он сам. Поэтому DXF теперь только читается,
а запись идёт так:

  1. Python читает DXF-копию, решает, что поменять, и запоминает правки
     по хэндлам объектов (хэндлы в DXF и DWG совпадают).
  2. Из правок собирается скрипт на AutoLISP: только хэндлы, имена шрифтов
     и inline-коды, без текстов надписей - поэтому в нём одна латиница
     и он одинаково работает в AutoCAD 2013...2027.
  3. AutoCAD открывает копию оригинального DWG, выполняет скрипт
     (entget/entmod - обычный API AutoCAD) и сохраняет под новым именем.
  4. Результат перечитывается и сверяется с тем, что должно было получиться.
"""

from __future__ import annotations

import math
import os
import re
import shutil

import dwg_normalizer as core

RESULT_RE = re.compile(r"DWGN-RESULT ok=(\d+) fail=(\d+) miss=(\d+) same=(\d+) locked=(\d+)")

# ---------------------------------------------------------------------------
# Что было до правок
# ---------------------------------------------------------------------------


def _xdata(entity) -> list | None:
    if not entity.has_xdata("ACAD"):
        return None
    return [(tag.code, tag.value) for tag in entity.get_xdata("ACAD")]


def snapshot(doc) -> dict:
    """Состояние полей, которые трогает нормализатор, по хэндлам."""
    styles = {}
    for s in doc.styles:
        styles[s.dxf.handle] = {
            "font": s.dxf.get("font", ""),
            "bigfont": s.dxf.get("bigfont", ""),
            "width": s.dxf.get("width", 1.0),
            "oblique": s.dxf.get("oblique", 0.0),
            "xdata": _xdata(s),
        }
    dimstyles = {d.dxf.handle: d.dxf.get("dimtxsty", "Standard") for d in doc.dimstyles}
    dims = {}
    for container in core.iter_containers(doc):
        for e in container:
            if e.dxftype() == "DIMENSION":
                dims[e.dxf.handle] = {"dimstyle": e.dxf.get("dimstyle", "Standard"),
                                      "acad": e.has_xdata("ACAD")}
    return {"styles": styles, "dimstyles": dimstyles, "dims": dims}


# ---------------------------------------------------------------------------
# Список правок
# ---------------------------------------------------------------------------


def build_ops(doc, before: dict, mtext_plan: dict) -> list[dict]:
    after = snapshot(doc)
    ops: list[dict] = []

    for h, a in after["styles"].items():
        b = before["styles"].get(h)
        if b is None:
            continue
        op: dict = {"kind": "style", "h": h}
        if a["font"] != b["font"]:
            op["font"] = a["font"]
        if a["bigfont"] != b["bigfont"]:
            op["bigfont"] = a["bigfont"]
        if a["width"] != b["width"]:
            op["width"] = float(a["width"])
        if a["oblique"] != b["oblique"]:
            op["oblique"] = math.radians(float(a["oblique"]))  # entmod ждёт радианы
        if a["xdata"] != b["xdata"]:
            op["xdata"] = a["xdata"] or []                   # [] - удалить
        if len(op) > 2:
            ops.append(op)

    for h, name in after["dimstyles"].items():
        if name != before["dimstyles"].get(h) and name in doc.styles:
            ops.append({"kind": "dimstyle_text", "h": h,
                        "sh": doc.styles.get(name).dxf.handle})

    for h, pairs in mtext_plan.items():
        ops.append({"kind": "mtext", "h": h, "pairs": pairs})

    for h, a in after["dims"].items():
        b = before["dims"].get(h)
        if b is None:
            continue
        restyle = a["dimstyle"] != b["dimstyle"]
        clear = b["acad"] and not a["acad"]
        if restyle or clear:
            sh = doc.dimstyles.get(a["dimstyle"]).dxf.handle if restyle else None
            ops.append({"kind": "dim", "h": h, "sh": sh, "clear": clear})
    return ops


# ---------------------------------------------------------------------------
# AutoLISP
# ---------------------------------------------------------------------------
# Только базовые функции AutoLISP (есть во всех версиях и в accoreconsole),
# без vl-/vla-: в консольном AutoCAD нет ActiveX. Без пустых строк: пустая
# строка в скрипте повторяет предыдущую команду. Без комментариев.

LISP_HELPERS = r'''
(setq dwgn:ok 0 dwgn:fail 0 dwgn:miss 0 dwgn:same 0 dwgn:locked nil)
(defun dwgn:mod (ed) (if (entmod ed) (setq dwgn:ok (1+ dwgn:ok)) (setq dwgn:fail (1+ dwgn:fail))))
(defun dwgn:put (k v ed) (if (assoc k ed) (subst (cons k v) (assoc k ed) ed) (append ed (list (cons k v)))))
(defun dwgn:name (h / e) (if (setq e (handent h)) (cdr (assoc 2 (entget e)))))
(defun dwgn:find (pat s i / n m r)
  (setq n (strlen s) m (strlen pat))
  (while (and (not r) (<= (+ i m -1) n))
    (if (= (substr s i m) pat) (setq r i) (setq i (1+ i))))
  r)
(defun dwgn:replace (s pairs / p pos)
  (foreach p pairs
    (setq pos 1)
    (while (setq pos (dwgn:find (car p) s pos))
      (setq s (strcat (substr s 1 (1- pos)) (cdr p) (substr s (+ pos (strlen (car p)))))
            pos (+ pos (strlen (cdr p))))))
  s)
(defun dwgn:mtext-get (ed / s g)
  (setq s "")
  (foreach g ed (if (= (car g) 3) (setq s (strcat s (cdr g)))))
  (strcat s (cdr (assoc 1 ed))))
(defun dwgn:mtext-set (ed s / out g)
  (foreach g ed
    (cond ((= (car g) 3))
          ((= (car g) 1)
           (while (> (strlen s) 250)
             (setq out (cons (cons 3 (substr s 1 250)) out) s (substr s 251)))
           (setq out (cons (cons 1 s) out)))
          (T (setq out (cons g out)))))
  (reverse out))
(defun dwgn:style (h font bigfont width obl xd / e ed)
  (if (setq e (handent h))
    (progn
      (setq ed (entget e))
      (if font (setq ed (dwgn:put 3 font ed)))
      (if bigfont (setq ed (dwgn:put 4 bigfont ed)))
      (if width (setq ed (dwgn:put 41 width ed)))
      (if obl (setq ed (dwgn:put 50 obl ed)))
      (if xd (setq ed (append ed (list (list -3 xd)))))
      (dwgn:mod ed))
    (setq dwgn:miss (1+ dwgn:miss))))
(defun dwgn:dimstyle-text (h sh / e ts)
  (if (and (setq e (handent h)) (setq ts (handent sh)))
    (dwgn:mod (dwgn:put 340 ts (entget e)))
    (setq dwgn:miss (1+ dwgn:miss))))
(defun dwgn:mtext (h pairs / e ed s new)
  (if (setq e (handent h))
    (progn
      (setq ed (entget e) s (dwgn:mtext-get ed) new (dwgn:replace s pairs))
      (if (= new s) (setq dwgn:same (1+ dwgn:same)) (dwgn:mod (dwgn:mtext-set ed new))))
    (setq dwgn:miss (1+ dwgn:miss))))
(defun dwgn:dim (h sh clear / e ed nm)
  (setq e (handent h))
  (if (and e sh) (if (not (setq nm (dwgn:name sh))) (setq e nil)))
  (if e
    (progn
      (setq ed (entget e))
      (if nm (setq ed (dwgn:put 3 nm ed)))
      (if clear (setq ed (append ed (list (list -3 (list "ACAD"))))))
      (dwgn:mod ed))
    (setq dwgn:miss (1+ dwgn:miss))))
(defun dwgn:unlock (/ r e ed)
  (setq r (tblnext "LAYER" T))
  (while r
    (if (= 4 (logand 4 (cdr (assoc 70 r))))
      (progn
        (setq e (tblobjname "LAYER" (cdr (assoc 2 r))) ed (entget e))
        (if (entmod (subst (cons 70 (- (cdr (assoc 70 ed)) 4)) (assoc 70 ed) ed))
          (setq dwgn:locked (cons e dwgn:locked)))))
    (setq r (tblnext "LAYER"))))
(defun dwgn:relock (/ e ed)
  (foreach e dwgn:locked
    (setq ed (entget e))
    (entmod (subst (cons 70 (logior 4 (cdr (assoc 70 ed)))) (assoc 70 ed) ed))))
'''


class NonAscii(ValueError):
    pass


def lstr(s: str) -> str:
    if not s.isascii():
        raise NonAscii(s)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lnum(x: float) -> str:
    text = f"{float(x):.12f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def lxdata(xd: list) -> str:
    items = ['"ACAD"']
    for code, value in xd:
        if isinstance(value, str):
            items.append(f"(cons {code} {lstr(value)})")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise NonAscii(repr(value))  # точки и прочее не переносим
        elif isinstance(value, int):
            items.append(f"(cons {code} {value})")
        else:
            items.append(f"(cons {code} {lnum(value)})")
    return "(list " + " ".join(items) + ")"


def render_op(op: dict) -> str:
    h = lstr(op["h"])
    kind = op["kind"]
    if kind == "style":
        font = lstr(op["font"]) if "font" in op else "nil"
        big = lstr(op["bigfont"]) if "bigfont" in op else "nil"
        width = lnum(op["width"]) if "width" in op else "nil"
        obl = lnum(op["oblique"]) if "oblique" in op else "nil"
        xd = lxdata(op["xdata"]) if "xdata" in op else "nil"
        return f"(dwgn:style {h} {font} {big} {width} {obl} {xd})"
    if kind == "dimstyle_text":
        return f"(dwgn:dimstyle-text {h} {lstr(op['sh'])})"
    if kind == "mtext":
        pairs = " ".join(f"(cons {lstr(a)} {lstr(b)})" for a, b in op["pairs"])
        return f"(dwgn:mtext {h} (list {pairs}))"
    if kind == "dim":
        sh = lstr(op["sh"]) if op["sh"] else "nil"
        return f"(dwgn:dim {h} {sh} {'T' if op['clear'] else 'nil'})"
    raise ValueError(kind)


def render_script(ops: list[dict], fmt: str, out_path: str) -> tuple[list[str], list[dict]]:
    """Строки скрипта для accoreconsole и правки, которые не удалось перенести."""
    lines = [ln for ln in LISP_HELPERS.strip().splitlines() if ln.strip()]
    lines.append("(dwgn:unlock)")
    skipped = []
    for op in ops:
        try:
            lines.append(render_op(op))
        except NonAscii:
            skipped.append(op)
    lines.append("(dwgn:relock)")
    lines.append('(princ (strcat "\\nDWGN-RESULT ok=" (itoa dwgn:ok) " fail=" (itoa dwgn:fail)'
                 ' " miss=" (itoa dwgn:miss) " same=" (itoa dwgn:same)'
                 ' " locked=" (itoa (length dwgn:locked)) "\\n"))')
    lines.append(f'(command "_.SAVEAS" "{fmt}" {lstr(out_path.replace(os.sep, "/"))})')
    return lines, skipped


# ---------------------------------------------------------------------------
# Применение и проверка
# ---------------------------------------------------------------------------

KIND_NAMES = {"style": "текстовых стилей", "dimstyle_text": "размерных стилей",
              "mtext": "MTEXT", "dim": "размеров"}


def apply_and_save(src: str, doc, before: dict, mtext_plan: dict, dst: str,
                   engine: dict, rep, verify: bool = True) -> None:
    ops = build_ops(doc, before, mtext_plan)
    rep.add()
    rep.add("--- Запись: правки вносит AutoCAD прямо в DWG ---")
    work = core._work_dir()
    try:
        inp = os.path.join(work, "in.dwg")
        shutil.copyfile(src, inp)
        fmt = core.check_format(core.dwg_version(inp), engine)
        out = os.path.join(work, "out.dwg")
        lines, skipped = render_script(ops, fmt, out)
        if skipped:
            kinds = ", ".join(sorted({KIND_NAMES[o["kind"]] for o in skipped}))
            rep.add(f"  ! пропущено правок с нелатинскими символами: {len(skipped)} ({kinds})")
        if len(ops) == len(skipped):
            shutil.copyfile(src, dst)
            rep.add("  править нечего, чертёж скопирован без изменений")
            return
        console = core._run_acad(engine["path"], inp, lines, work, out, "внесение правок")

        m = RESULT_RE.search(console)
        if m:
            ok, fail, miss, same, locked = (int(x) for x in m.groups())
            rep.add(f"  применено правок: {ok} из {len(ops) - len(skipped)}")
            if locked:
                rep.add(f"  слоёв временно разблокировано и снова заблокировано: {locked}")
            if fail:
                rep.add(f"  ! AutoCAD отказался менять объектов: {fail}")
            if miss:
                rep.add(f"  ! не найдено объектов по хэндлу: {miss}")
            if same:
                rep.add(f"  уже были в нужном виде: {same}")
        else:
            rep.add("  ! AutoCAD сохранил чертёж, но не сообщил итог правок")
        try:
            shutil.copyfile(out, dst)
        except PermissionError:
            raise RuntimeError(f"Не удалось записать {dst}: файл открыт в AutoCAD? "
                               "Закрой его и запусти ещё раз.") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if verify:
        verify_result(dst, doc, [o for o in ops if o not in skipped], engine, rep)


def verify_result(dst: str, expected_doc, ops: list[dict], engine: dict, rep) -> None:
    """Перечитывает сохранённый чертёж и сверяет его с ожидаемым."""
    rep.add()
    rep.add("--- Проверка результата ---")
    try:
        res = core.open_drawing(dst, engine)
    except Exception as exc:  # noqa: BLE001
        rep.add(f"  ! не удалось перечитать результат: {exc}")
        return

    problems = []
    for op in ops:
        h = op["h"]
        exp, got = expected_doc.entitydb.get(h), res.entitydb.get(h)
        if got is None:
            problems.append(f"{h}: объект пропал")
            continue
        kind = op["kind"]
        if kind == "style":
            if "font" in op and got.dxf.get("font", "").lower() != op["font"].lower():
                problems.append(f"стиль {got.dxf.name}: шрифт {got.dxf.get('font', '')!r}")
            if "xdata" in op and _xdata(got) != (op["xdata"] or None):
                fam = got.get_extended_font_data()[0] if got.has_xdata("ACAD") else ""
                problems.append(f"стиль {got.dxf.name}: семейство {fam!r}")
        elif kind == "mtext":
            if got.text != exp.text:
                problems.append(f"MTEXT {h}: {got.text[:50]!r}")
        elif kind == "dim":
            if op["sh"] and got.dxf.get("dimstyle") != exp.dxf.get("dimstyle"):
                problems.append(f"размер {h}: стиль {got.dxf.get('dimstyle')!r}")
            if op["clear"] and got.has_xdata("ACAD") and got.override().dimstyle_attribs:
                problems.append(f"размер {h}: переопределения остались")
    if problems:
        rep.add(f"  ! не совпало: {len(problems)}")
        for p in problems[:8]:
            rep.add(f"    {p}")
    else:
        rep.add(f"  все {len(ops)} правок на месте")
