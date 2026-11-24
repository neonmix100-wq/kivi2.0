"""Одноразовая чистка оранжевых литералов в renderer.

Палитра в theme.js/theme.css задана безопасно (split/space rgb), поэтому
скрипт можно прогонять по всем renderer-файлам без поломки пресетов.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "app" / "renderer"
HEX = ["#f0703c", "#ff7a45", "#f2703c", "#ff6a3d", "#ef7038", "#e05f2a"]


def rgb_repl(m):
    r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if r >= 200 and 80 <= g <= 150 and b <= 100:
        return "var(--accent)"
    return m.group(0)


total = 0
for f in ROOT.glob("*"):
    if f.suffix not in (".css", ".js", ".html"):
        continue
    t = f.read_text(encoding="utf-8")
    o = t
    for h in HEX:
        t = re.sub(re.escape(h), "var(--accent)", t, flags=re.I)
    t = re.sub(
        r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,[^)]*)?\)",
        rgb_repl,
        t,
    )
    t = re.sub(
        r"hsla?\(\s*(?:1[0-9]|2[0-5])\s*,\s*[6-9]\d%\s*,\s*[4-6]\d%\s*(?:,[^)]*)?\)",
        "var(--accent)",
        t,
    )
    if t != o:
        f.write_text(t, encoding="utf-8")
        total += 1
        print("patched", f.name)
print("files patched:", total)
