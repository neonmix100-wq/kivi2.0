"""Смоук единой темы, прозрачности и фильтра консоли тестов."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "app" / "renderer"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def main() -> int:
    print("=== SMOKE: theme / transparency / test console ===")
    ok = True
    theme_css = RENDERER / "theme.css"
    theme_js = RENDERER / "theme.js"
    app_js = (RENDERER / "app.js").read_text(encoding="utf-8")
    styles_css = (RENDERER / "styles.css").read_text(encoding="utf-8")
    index_html = (RENDERER / "index.html").read_text(encoding="utf-8")
    main_js = (ROOT / "app" / "main.js").read_text(encoding="utf-8")
    js = theme_js.read_text(encoding="utf-8") if theme_js.is_file() else ""

    print("\n--- 1) theme files ---")
    ok &= check("theme.css существует", theme_css.is_file())
    ok &= check("theme.js существует", theme_js.is_file())
    ok &= check("PRESETS есть", "const PRESETS" in js)
    for preset in ("dark", "light", "green", "blue", "red"):
        ok &= check(f"preset {preset}", re.search(rf"\b{preset}\s*:\s*\{{", js) is not None)
    for fn in ("function apply(", "function randomize(", "function exportTheme(", "function importTheme("):
        ok &= check(f"JS {fn}", fn in js)
    ok &= check("localStorage key", "kivi.theme.v2" in js)

    print("\n--- 2) index theme editor ---")
    ok &= check("theme-editor", 'id="theme-editor"' in index_html)
    color_count = len(re.findall(r"data-theme-key=", index_html))
    ok &= check("7 color inputs", color_count == 7, str(color_count))
    for marker in (
        "data-theme-alpha",
        "data-theme-radius",
        "data-theme-font",
        "data-theme-sync",
        "theme-reset",
        "theme-random",
        "theme-export",
        "theme-import",
    ):
        ok &= check(f"HTML {marker}", marker in index_html)

    print("\n--- 3) hardcoded accent removed ---")
    combined = "\n".join((app_js, styles_css, index_html))
    ok &= check("нет #f0703c вне theme.*", "#f0703c" not in combined.lower())
    ok &= check("нет #ff7a45 вне theme.*", "#ff7a45" not in combined.lower())

    print("\n--- 4) test console ---")
    ok &= check("startTestUI", "function startTestUI(name)" in app_js)
    ok &= check(
        "фильтр msg.name !== currentTest",
        re.search(r"msg\.name\s*!==\s*currentTest", app_js) is not None,
    )
    ok &= check("tests-title", 'id="tests-title"' in index_html)
    ok &= check("очистка tests-output", "out.textContent = ''" in app_js)

    print("\n--- 5) transparency ---")
    ok &= check("main.js transparent:true", "transparent: true" in main_js)
    ok &= check("main.js прозрачный backgroundColor", "#00000000" in main_js)
    ok &= check("main.js fallback setOpacity", "setOpacity" in main_js)

    print()
    print("PASS" if ok else "FAIL — скопируй вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
