import os, requests
from dotenv import load_dotenv

load_dotenv()
base, key = os.getenv("LLM_BASE_URL"), os.getenv("LLM_API_KEY")
r = requests.get(f"{base}/models",
                 headers={"Authorization": f"Bearer {key}"}, timeout=30)
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:500])
    raise SystemExit(1)

ids = sorted(m["id"] for m in r.json().get("data", []))
print("Всего моделей:", len(ids))
for kw in ["deepseek", "hydrogen", "sonnet", "terra",
           "speech", "tts", "gigaam", "whisper", "minimax"]:
    hits = [i for i in ids if kw in i.lower()]
    print(f"\n=== {kw} ({len(hits)}) ===")
    for i in hits[:12]:
        print("  ", i)