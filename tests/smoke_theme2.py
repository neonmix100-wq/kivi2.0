"""Смоук реальной packaged-сборки: редактор темы и пузыри внутри app.asar.

Запуск:
    python tests/smoke_theme2.py

Если проверка asar упала — сначала пересобери приложение:
    cd app
    npm run dist
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "app" / "renderer"
PATCH = ROOT / "tools" / "patch_theme.py"
ASAR = ROOT / "app" / "dist" / "win-unpacked" / "resources" / "app.asar"
ORANGE_HEX = ("#f0703c", "#ff7a45", "#f2703c", "#ff6a3d", "#ef7038", "#e05f2a")


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


def orange_literals(text: str) -> list[str]:
    """Оранжевые hex/rgba/hsla, которые не должны оставаться в renderer."""
    found = [item for item in ORANGE_HEX if item in text.lower()]
    for match in re.finditer(
        r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,[^)]*)?\)",
        text,
        re.I,
    ):
        r, g, b = (int(match.group(i)) for i in (1, 2, 3))
        if r >= 200 and 80 <= g <= 150 and b <= 100:
            found.append(match.group(0))
    for match in re.finditer(
        r"hsla?\(\s*(?:1[0-9]|2[0-5])\s*,\s*[6-9]\d%\s*,\s*[4-6]\d%\s*(?:,[^)]*)?\)",
        text,
        re.I,
    ):
        found.append(match.group(0))
    return found


def main() -> int:
    print("=== SMOKE: theme2 / packaged app.asar ===")
    ok = True

    print("\n--- 1) patch_theme.py ---")
    ok &= check("tools/patch_theme.py существует", PATCH.is_file())
    if PATCH.is_file():
        result = subprocess.run(
            [sys.executable, str(PATCH)],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=30,
        )
        print(result.stdout.strip() or result.stderr.strip())
        ok &= check("patch_theme.py завершился успешно", result.returncode == 0)

    print("\n--- 2) renderer: нет оранжевых литералов ---")
    for path in sorted(RENDERER.glob("*")):
        if path.suffix not in (".css", ".js", ".html"):
            continue
        found = orange_literals(path.read_text(encoding="utf-8"))
        ok &= check(f"{path.name}: чисто", not found, ", ".join(found[:3]))

    print("\n--- 3) runtime-редактор и перекраска пузырей ---")
    theme_js = (RENDERER / "theme.js").read_text(encoding="utf-8")
    app_js = (RENDERER / "app.js").read_text(encoding="utf-8")
    main_js = (ROOT / "app" / "main.js").read_text(encoding="utf-8")
    for marker in ("ensureEditor", "paintBubbles", "MutationObserver", "startBubbleObserver"):
        ok &= check(f"theme.js: {marker}", marker in theme_js)
    ok &= check("app.js: el.dataset.role = turn.role", "el.dataset.role = turn.role" in app_js)
    ok &= check("app.js: класс bubble", "classList.add('bubble'" in app_js)
    ok &= check("main.js: ipcMain.on(\"theme-alpha\"", 'ipcMain.on("theme-alpha"' in main_js)

    print("\n--- 4) packaged app.asar ---")
    if not ASAR.is_file():
        ok &= check("app.asar существует", False, f"нет {ASAR}; сначала cd app && npm run dist")
    else:
        blob = ASAR.read_bytes()
        for marker in (b"theme-editor", b"data-theme-alpha", b"paintBubbles", b"theme-random"):
            ok &= check(f"asar содержит {marker.decode()}", marker in blob)

    print()
    print("PASS" if ok else "FAIL — пересобери: cd app; npm run dist")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
