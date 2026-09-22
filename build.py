"""Сборка DWG Normalizer в готовую программу (Windows).

    pip install pyinstaller
    python build.py

Результат: dist\\DWG Normalizer\\DWG Normalizer.exe - запускается на компьютере,
где нет ни Python, ни библиотек. Для работы с DWG нужен только установленный
AutoCAD (2013 или новее), который и так есть у всех.

Этот же скрипт запускает GitHub Actions при сборке установщика.
"""

import os
import shutil
import sys

import PyInstaller.__main__

APP = "DWG Normalizer"
HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist", APP)


def main() -> None:
    PyInstaller.__main__.run([
        os.path.join(HERE, "gui.py"),
        "--name", APP,
        "--windowed",            # без чёрного окна консоли
        "--noconfirm", "--clean",
        "--collect-data", "ezdxf",   # ezdxf тащит с собой служебные данные
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
    ])

    # профили кладём рядом с exe, чтобы их можно было править без пересборки
    copied = []
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".json"):
            shutil.copy2(os.path.join(HERE, name), DIST)
            copied.append(name)

    print(f"\nГотово: {DIST}")
    print(f"Профили рядом с программой: {', '.join(copied) or 'нет'}")
    if sys.platform != "win32":
        print("ВНИМАНИЕ: сборка не под Windows - .exe получается только на Windows.")


if __name__ == "__main__":
    main()
