import os, subprocess, base64, json
import requests
from dotenv import load_dotenv

load_dotenv()
base, key = os.getenv("LLM_BASE_URL"), os.getenv("LLM_API_KEY")
h = {"Authorization": f"Bearer {key}"}
text = "Привет, Лёша. Я Киви, твой голосовой ассистент. Готова к работе."

# (model_id, label, voice_id, response_format)
candidates = [
    ("minimax/speech-2.8-turbo",    "1_minimax_turbo",    "female-shaonv",    "mp3"),
    ("minimax/speech-2.8-hd",       "2_minimax_hd",       "female-shaonv",    "mp3"),
    ("openai/gpt-4o-mini-tts",      "3_gpt4o_shimmer",    "shimmer",          "mp3"),
    ("google/gemini-3.8-flash-tts", "4_gemini_flash",     "Leda",             "mp3"),
    ("microsoft/mai-voice-2",       "5_mai_voice",        None,               "mp3"),
]

for i, (mid, label, voice, resp_fmt) in enumerate(candidates, 1):
    print(f"\n[{i}/5] {label}")
    body = {"model": mid, "input": text}
    if voice:
        body["voice"] = voice
    if resp_fmt:
        body["response_format"] = resp_fmt

    try:
        r = requests.post(f"{base}/audio/speech", headers=h, json=body, timeout=120)
    except Exception as e:
        print("  TIMEOUT:", type(e).__name__); continue

    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}"); continue

    raw = r.content
    path_mp3 = f"data/{label}.mp3"

    # Проверка: JSON с base64?
    try:
        j = json.loads(raw)
        if "audio" in j:
            audio_bytes = base64.b64decode(j["audio"])
            open(path_mp3, "wb").write(audio_bytes)
            print(f"  ✓ JSON->base64->mp3: {path_mp3} ({len(audio_bytes)} байт)")
            continue
    except (json.JSONDecodeError, KeyError):
        pass

    # Иначе — это сырой mp3/wav/...
    open(path_mp3, "wb").write(raw)
    print(f"  ✓ сырой файл: {path_mp3} ({len(raw)} байт)")

print("\n" + "="*60)
print("Открой D:\\Kivi\\data\\ и послушай файлы 1..5.mp3")
print("="*60)