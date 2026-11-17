import os, requests
from dotenv import load_dotenv
load_dotenv()
base, key = os.getenv("LLM_BASE_URL"), os.getenv("LLM_API_KEY")
h = {"Authorization": f"Bearer {key}"}

r = requests.get(f"{base}/models", headers=h, timeout=30)
print("Ключ:", "OK" if r.ok else f"ОШИБКА {r.status_code}",
      "| доступно моделей:", len(r.json().get("data", [])))

r = requests.post(f"{base}/chat/completions", headers=h, timeout=60, json={
    "model": os.getenv("LLM_MODEL_CHEAP"),
    "messages": [{"role": "user", "content": "Ответь ровно: Киви на связи"}],
    "max_tokens": 30})
d = r.json()
print("Ответ:", d["choices"][0]["message"]["content"])
print("Потрачено токенов:", d.get("usage"))