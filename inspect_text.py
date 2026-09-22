"""
Диагностика одной надписи: где она лежит и что определяет её шрифт.

Запуск (из папки проекта, с активным .venv):
    python inspect_text.py "путь\\к\\чертежу.dwg" "армирования"

Ищет текст по фрагменту везде: модель, листы, блоки, атрибуты вставок.
"""

import sys

import dwg_normalizer as core  # открытие DWG и автопоиск ODA берём из ядра


def style_info(doc, name: str) -> str:
    if name not in doc.styles:
        return f"стиль '{name}' в чертеже не найден"
    s = doc.styles.get(name)
    family, italic, bold = s.get_extended_font_data()
    return (f"стиль '{name}':\n"
            f"      файл шрифта         = '{s.dxf.get('font', '')}'\n"
            f"      семейство (XDATA)   = '{family}'  bold={bold} italic={italic}\n"
            f"      ширина / наклон     = {s.dxf.get('width', 1.0)} / {s.dxf.get('oblique', 0.0)}")


def text_of(e):
    """Возвращает (сырой текст с кодами, чистый текст) или None, если это не текст."""
    kind = e.dxftype()
    if kind == "MTEXT":
        return e.text, e.plain_text()
    if kind in ("TEXT", "ATTRIB", "ATTDEF"):
        if kind != "TEXT" and e.has_embedded_mtext_entity:  # многострочный атрибут
            return e.virtual_mtext_entity().text, e.plain_mtext()
        return e.dxf.text, e.dxf.text
    return None


def main() -> None:
    if len(sys.argv) < 3:
        print('Использование: python inspect_text.py "чертеж.dwg" "фрагмент текста"')
        return
    path, needle = sys.argv[1], sys.argv[2].casefold()
    doc = core.open_drawing(path)

    # *Model_Space / *Paper_Space -> человеческие имена
    layout_names = {lay.block_record_name: f"лист '{lay.name}'" for lay in doc.layouts}
    layout_names["*Model_Space"] = "модель"

    found = 0
    for block in doc.blocks:
        where = layout_names.get(block.name, f"блок '{block.name}'")
        for e in block:
            candidates = [(e, where)]
            if e.dxftype() == "INSERT":
                candidates += [(a, f"{where} -> вставка '{e.dxf.name}'") for a in e.attribs]
            for t, place in candidates:
                res = text_of(t)
                if not res or needle not in res[1].casefold():
                    continue
                raw, _ = res
                found += 1
                style = t.dxf.get("style", "Standard")
                print(f"\n#{found}  {t.dxftype()}  [{place}]")
                print(f"   сырой текст: {raw[:200]!r}")
                print(f"   inline-шрифт: {core.RE_FONT.findall(raw) or 'нет'}")
                print(f"   inline-ширина: {core.RE_WIDTH.findall(raw) or 'нет'}")
                if t.dxftype() in ("TEXT", "ATTRIB", "ATTDEF"):
                    print(f"   ширина самой надписи: {t.dxf.get('width', 1.0)}")
                print(f"   {style_info(doc, style)}")

    if not found:
        print(f"Текст с фрагментом '{sys.argv[2]}' не найден. "
              "Попробуй фрагмент короче, например одно слово.")


if __name__ == "__main__":
    main()
