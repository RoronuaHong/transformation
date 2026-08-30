import requests

r = requests.post(
    "http://127.0.0.1:8910/ask",
    json={"query": "一桌 Deskline 有哪些职场资产包？分别多少钱？", "top_k": 5},
    timeout=300,
)
data = r.json()
with open("ask_out.txt", "w", encoding="utf-8") as f:
    f.write(f"status: {r.status_code}\n")
    f.write("answer:\n" + data.get("answer", "") + "\n")
    f.write("sources: " + repr(data.get("sources")) + "\n")
print("written to ask_out.txt")
