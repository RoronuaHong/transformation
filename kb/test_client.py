"""快速验证 /health 与 /search。"""
import requests

base = "http://127.0.0.1:8910"
print("health:", requests.get(f"{base}/health").json())
r = requests.post(
    f"{base}/search",
    json={"query": "周报怎么写", "top_k": 3},
)
print("search status:", r.status_code)
data = r.json()
for i, res in enumerate(data.get("results", [])):
    print(f"--- result {i+1} (score={res['score']:.3f}, source={res['source']}) ---")
    print(res["text"][:200])
