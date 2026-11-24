"""Захват экрана и подготовка PNG для vision-тира."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from io import BytesIO
from typing import Any

from PIL import Image
from loguru import logger

from kivi.config import DATA_DIR, get_settings

__all__ = ["capture_screen", "prepare"]

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
MAX_SIDE_DEFAULT = 1280
LAST_SCREEN_PATH = DATA_DIR / "tmp" / "last_screen.png"


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", _RGBQUAD * 1),
    ]


def _set_dpi_aware() -> None:
    """Не дать Windows отдать логический размер вместо физических пикселей."""
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:  # noqa: BLE001 - ниже есть старый fallback
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - не критично для захвата
        pass


def _winapi() -> tuple[Any, Any]:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    return user32, gdi32


def capture_screen() -> Image.Image:
    """Снять основной монитор через GDI BitBlt и вернуть RGB PIL.Image."""
    _set_dpi_aware()
    user32, gdi32 = _winapi()
    width = int(user32.GetSystemMetrics(SM_CXSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYSCREEN))
    if width <= 0 or height <= 0:
        raise RuntimeError("скриншот: Windows вернул пустой размер экрана")

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise RuntimeError("скриншот: не удалось получить DC экрана")
    memory_dc = None
    bitmap = None
    old_object = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise RuntimeError("скриншот: не удалось создать совместимый DC")
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            raise RuntimeError("скриншот: не удалось создать bitmap")
        old_object = gdi32.SelectObject(memory_dc, bitmap)
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            0,
            0,
            SRCCOPY | CAPTUREBLT,
        ):
            raise RuntimeError("скриншот: BitBlt не смог скопировать экран")

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(info),
            DIB_RGB_COLORS,
        ):
            raise RuntimeError("скриншот: GetDIBits не вернул пиксели")
        image = Image.frombuffer(
            "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
        ).convert("RGB")
        logger.debug("скриншот: захвачено {}x{}", width, height)
        return image
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def _vision_config() -> dict[str, Any]:
    try:
        section = get_settings().section("vision")
        return dict(section or {})
    except Exception as exc:  # noqa: BLE001 - конфиг не должен ломать захват
        logger.warning("скриншот: vision.* не прочитан: {}", exc)
        return {}


def _save_last_enabled() -> bool:
    return bool(_vision_config().get("save_last", False))


def prepare(
    image: Image.Image,
    max_side: int = MAX_SIDE_DEFAULT,
    *,
    save_last: bool | None = None,
) -> bytes:
    """Уменьшить изображение и вернуть PNG bytes; по умолчанию на диск не писать."""
    original_w, original_h = image.size
    limit = max(1, int(max_side))
    work = image.convert("RGB")
    if max(work.size) > limit:
        work.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    out = BytesIO()
    work.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    logger.info(
        "скриншот: {}x{} -> {}x{}, {} КБ",
        original_w,
        original_h,
        work.width,
        work.height,
        max(1, round(len(data) / 1024)),
    )
    should_save = _save_last_enabled() if save_last is None else bool(save_last)
    if should_save:
        try:
            LAST_SCREEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAST_SCREEN_PATH.write_bytes(data)
            logger.info("скриншот: сохранён для отладки {}", LAST_SCREEN_PATH)
        except OSError as exc:
            logger.warning("скриншот: не удалось сохранить {}: {}", LAST_SCREEN_PATH, exc)
    return data

    return user32, gdi32
