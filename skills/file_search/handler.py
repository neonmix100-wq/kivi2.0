"""Скилл file_search: файлы по имени внутри разрешённых папок.

Обход — только через ctx.fs_list(): брокер сам проверяет, что папка входит
в permissions.filesystem, и скрывает .env* и базы данных проекта.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

ROOTS = (r"D:\Kivi\data", r"D:\Kivi\skills")
MAX_RESULTS = 5


def handle(ctx, pattern: str = "*", **_: object) -> str:
    """Найти до 5 файлов по маске (fnmatch) среди разрешённых папок."""
    mask = " ".join(str(pattern or "*").split()) or "*"
    ctx.log(f"ищу {mask!r} в {ROOTS}")

    found: list[str] = []
    for root in ROOTS:
        for path in ctx.fs_list(root, recursive=True):
            if not fnmatch(Path(path).name.lower(), mask.lower()):
                continue
            found.append(str(Path(path).relative_to(ctx.project_root)))
            if len(found) >= MAX_RESULTS:
                break
        if len(found) >= MAX_RESULTS:
            break

    if not found:
        return f"Ничего не нашла по шаблону «{mask}»."
    lines = [f"Нашла {len(found)}:"]
    lines.extend(f"  {item}" for item in found)
    return "\n".join(lines)
