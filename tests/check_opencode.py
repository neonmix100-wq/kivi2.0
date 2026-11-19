import os, uuid, requests
from dotenv import load_dotenv

load_dotenv()
key = (os.getenv("OPENCODE_API_KEY") or "").strip()
if not key:
    raise SystemExit("Нет OPENCODE_API_KEY в .env")

BASES = [
    "https://opencode.ai/zen/v1",
    "https://opencode.ai/inference/openai/v1",
]
H = {
    "Authorization": f"Bearer {key}",
    "User-Agent": "kivi-assistant/0.1",
    "x-opencode-session": str(uuid.uuid4()),
}

for base in BASES:
    print(f"\n=== {base} ===")
    try:
        r = requests.get(f"{base}/models", headers=H, timeout=30)
        print("models:", r.status_code)
        if r.ok:
            ids = sorted(m["id"] for m in r.json().get("data", []))
            free = [i for i in ids if "free" in i.lower() or "pickle" in i.lower()]
            print("всего:", len(ids), "| бесплатных:", len(free))
            for i in free:
                print("  ", i)
        else:
            print(r.text[:200]); continue
    except Exception as e:
        print("ERR", type(e).__name__); continue

    for model in ["deepseek-v4.1-flash-free", "hy4-flash-free"]:
        try:
            r = requests.post(f"{base}/chat/completions", headers=H, timeout=90, json={
                "model": model,
                "messages": [{"role": "user", "content": "Ответь ровно: Киви на связи"}],
                "max_tokens": 30,
            })
            if r.ok:
                d = r.json()
                print(f"  {model}: OK ->", d["choices"][0]["message"]["content"][:60])
            else:
                print(f"  {model}: HTTP {r.status_code} {r.text[:160]}")
        except Exception as e:
            print(f"  {model}: ERR {type(e).__name__}")