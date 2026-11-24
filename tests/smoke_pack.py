"""Смоук упаковки UI: package.json, иконка, kiviHome и spawn (без сборки).

Запуск:
    python tests/smoke_pack.py
"""

from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = PROJECT_ROOT / "app"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a valid PNG")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def main() -> int:
    ok = True
    package_path = APP / "package.json"
    main_path = APP / "main.js"
    index_path = APP / "renderer" / "index.html"
    icon_path = APP / "build" / "icon.png"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    main_js = main_path.read_text(encoding="utf-8")
    index_html = index_path.read_text(encoding="utf-8")

    print("=== SMOKE: Windows packaging ===")
    print("\n--- 1) package.json ---")
    dev = package.get("devDependencies") or {}
    scripts = package.get("scripts") or {}
    build = package.get("build") or {}
    ok &= check("electron-builder в devDependencies", "electron-builder" in dev, str(dev))
    ok &= check("script dist", scripts.get("dist") == "electron-builder --win")
    ok &= check(
        "script pack",
        scripts.get("pack") == "electron-builder --win --dir",
    )
    ok &= check("appId", build.get("appId") == "ai.kivi.desktop")
    ok &= check("productName", build.get("productName") == "Kivi")
    dirs = build.get("directories") or {}
    ok &= check("output=dist", dirs.get("output") == "dist")
    ok &= check("buildResources=build", dirs.get("buildResources") == "build")
    files = build.get("files") or []
    ok &= check(
        "files: main/preload/renderer/package",
        all(item in files for item in ("main.js", "preload.js", "renderer/**", "package.json")),
        str(files),
    )
    win = build.get("win") or {}
    ok &= check("targets nsis + portable", win.get("target") == ["nsis", "portable"])
    ok &= check("win icon", win.get("icon") == "build/icon.png")
    nsis = build.get("nsis") or {}
    ok &= check("nsis oneClick=false", nsis.get("oneClick") is False)
    ok &= check(
        "nsis allow dir",
        nsis.get("allowToChangeInstallationDirectory") is True,
    )
    ok &= check("nsis desktop shortcut", nsis.get("createDesktopShortcut") is True)
    ok &= check("nsis shortcutName", nsis.get("shortcutName") == "Kivi")
    ok &= check("nsis perMachine=false", nsis.get("perMachine") is False)

    print("\n--- 2) icon ---")
    ok &= check("build/icon.png существует", icon_path.is_file())
    if icon_path.is_file():
        width, height = png_size(icon_path)
        ok &= check("icon 512x512", (width, height) == (512, 512), f"{width}x{height}")
    icon_script = APP / "build" / "make_icon.ps1"
    ok &= check("скрипт иконки есть", icon_script.is_file())

    print("\n--- 3) kiviHome и spawn ---")
    ok &= check("env KIVI_HOME", "KIVI_HOME" in main_js)
    ok &= check("kivi-home.json", "kivi-home.json" in main_js)
    ok &= check("default D:\\Kivi", "D:\\\\Kivi" in main_js)
    ok &= check(
        "python = join(kiviHome, .venv, Scripts, python.exe)",
        bool(
            re.search(
                r"path\.join\(\s*kiviHome\s*,\s*['\"]\.venv['\"]\s*,\s*['\"]Scripts['\"]\s*,\s*['\"]python\.exe['\"]",
                main_js,
            )
        ),
    )
    ok &= check("spawn cwd = kiviHome", "cwd: kiviHome" in main_js)
    ok &= check("spawn не использует PROJECT_ROOT", "PROJECT_ROOT" not in main_js)
    spawn_slice = main_js.split("spawn(python", 1)[1].split("});", 1)[0]
    ok &= check("spawn не содержит __dirname", "__dirname" not in spawn_slice)

    print("\n--- 4) packaged UI ---")
    ok &= check("devtools закрыты в packaged", "devTools: !app.isPackaged" in main_js)
    ok &= check("лог userData/kivi-app.log", "kivi-app.log" in main_js)
    ok &= check("renderer: styles.css относительный", 'href="styles.css"' in index_html)
    ok &= check("renderer: app.js относительный", 'src="app.js"' in index_html)
    ok &= check("renderer без D:\\Kivi\\app", "D:\\\\Kivi\\\\app" not in index_html)

    print()
    print("PASS" if ok else "FAIL — скопируй вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
