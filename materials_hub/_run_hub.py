"""Materials Hub 启动入口（由 pm2 的 vitual-hub-api 调用）。

端口不在这里硬编码：server.py 读取 VITUAL_HUB_PORT（pm2 env 注入，默认 3402），
见 ecosystem.config.cjs。旧名 _run_8200.py 是 8000 被 FunUnion API 占用时的
临时产物，已随端口迁移废弃。
"""
import os

# 关掉 stage-2 重排(cross-encoder reranker)：本机对 15 条素材做 bge-reranker-v2-m3
# 重排要 ~7.5s，语义检索直接卡死；关掉后走 bi-encoder 向量 + RRF 融合，延迟降到 ~50ms，
# 对小规模素材库质量无损。需要重排时删掉这行即可(VITUAL_RERANK_MODEL 留空=按模型可用性自动)。
os.environ["VITUAL_RERANK_MODEL"] = "__none__"

import server

server.main()
