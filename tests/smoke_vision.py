"""Смоук vision: prepare/screen_look/Router/planner и save_last."""
from __future__ import annotations

import base64
import json
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.vision.screen as screen_mod
from PIL import Image
from kivi.log import setup_logging
from kivi.skills.broker import SkillContext, set_vision_router
from kivi.skills.gaps import GapLog
from kivi.skills.loader import load
from kivi.skills.planner import Planner
from kivi.skills.registry import SkillRegistry

TMP = PROJECT_ROOT / "data" / "tmp"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class VisionRouter:
    """Мок Router: запоминает messages и task_type."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Any,
        task_type: str = "chat",
        verify: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {"messages": messages, "task_type": task_type, "kwargs": kwargs}
        )
        return {
            "text": "На экране открыто приложение, видна строка поиска и ошибка.",
            "tokens_in": 120,
            "tokens_out": 30,
            "cost_rub": 0.012,
            "ok": True,
        }


class JsonRouter:
    """Мок Router для planner."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete(self, messages: Any, task_type: str = "intent", **kwargs: Any):
        return {
            "text": json.dumps(self.payload, ensure_ascii=False),
            "cost_rub": 0.0,
            "ok": True,
        }


def main() -> int:
    setup_logging()
    print("=== SMOKE: vision screen_look ===")
    ok = True
    TMP.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / f"vision_installed_{token}",
        db_path=TMP / f"vision_skills_{token}.db",
    )
    registry.scan()
    manifest = registry.get("screen_look")
    ok &= check("манифест screen_look найден", manifest is not None)
    if manifest is None:
        print("FAIL — нет screen_look")
        return 1

    synthetic = Image.new("RGB", (2000, 1000), (24, 32, 44))
    old_capture = screen_mod.capture_screen
    old_last = screen_mod.LAST_SCREEN_PATH
    screen_mod.capture_screen = lambda: synthetic.copy()  # type: ignore[assignment]
    screen_mod.LAST_SCREEN_PATH = TMP / f"last_screen_{token}.png"
    router = VisionRouter()
    set_vision_router(router)
    try:
        print("\n--- 1) prepare: downscale + PNG ---")
        png = screen_mod.prepare(synthetic, max_side=640, save_last=False)
        image = Image.open(BytesIO(png))
        print("    PNG:", image.size, image.mode, len(png), "bytes")
        ok &= check("PNG-сигнатура", png[:8] == b"\x89PNG\r\n\x1a\n")
        ok &= check("даунскейл до max_side", max(image.size) == 640, str(image.size))
        ok &= check(
            "нет файла при save_last=False",
            not screen_mod.LAST_SCREEN_PATH.exists(),
        )

        print("\n--- 2) screen_look -> vision Router ---")
        handler = load(manifest)
        answer = handler(SkillContext(manifest))
        print("    answer:", answer)
        ok &= check(
            "ответ — мок-описание",
            "На экране открыто приложение" in answer,
            answer,
        )
        ok &= check("Router вызван", len(router.calls) == 1)

        if router.calls:
            call = router.calls[0]
            ok &= check("task_type=vision", call["task_type"] == "vision")
            content = call["messages"][0]["content"]
            image_part = next(
                (part for part in content if part.get("type") == "image_url"), {}
            )
            url = str((image_part.get("image_url") or {}).get("url") or "")
            print("    image_url:", url[:32], "...", len(url), "chars")
            ok &= check(
                "base64 data-url PNG",
                url.startswith("data:image/png;base64,")
                and base64.b64decode(url.split(",", 1)[1])[:8]
                == b"\x89PNG\r\n\x1a\n",
            )
            ok &= check(
                "промпт просит 2-4 предложения",
                any(
                    part.get("type") == "text"
                    and "2-4 предложения" in str(part.get("text"))
                    for part in content
                ),
            )

        print("\n--- 3) save_last=True ---")
        screen_mod.prepare(synthetic, max_side=320, save_last=True)
        ok &= check(
            "файл создан при save_last=True",
            screen_mod.LAST_SCREEN_PATH.is_file()
            and screen_mod.LAST_SCREEN_PATH.read_bytes()[:8]
            == b"\x89PNG\r\n\x1a\n",
        )

        print("\n--- 4) planner -> screen_look ---")
        planner = Planner(
            registry=registry,
            strategy="llm_only",
            router=JsonRouter(
                {
                    "skill_name": "screen_look",
                    "args": {},
                    "route": "skill",
                    "missing": [],
                }
            ),
            gaps=GapLog(TMP / f"vision_gaps_{token}.db"),
        )
        plan = planner.plan("что на экране")
        actual = getattr(plan.get("skill"), "name", None)
        print("    planner:", actual, plan.get("route"))
        ok &= check(
            "planner «что на экране» -> screen_look",
            actual == "screen_look" and plan.get("route") == "skill",
            str(actual),
        )

        print("\n--- 5) UI Обзор ---")
        html = (PROJECT_ROOT / "app" / "renderer" / "index.html").read_text(
            encoding="utf-8"
        )
        js = (PROJECT_ROOT / "app" / "renderer" / "app.js").read_text(
            encoding="utf-8"
        )
        ok &= check("кнопка «Что на экране?»", 'id="btn-screen-look"' in html)
        ok &= check(
            "кнопка отправляет send_text",
            "text: 'что на экране'" in js and "btn-screen-look" in js,
        )
    finally:
        screen_mod.capture_screen = old_capture  # type: ignore[assignment]
        screen_mod.LAST_SCREEN_PATH = old_last
        set_vision_router(None)

    print()
    print("PASS" if ok else "FAIL — скопируй вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
