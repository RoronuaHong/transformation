import os

# 关掉 stage-2 重排(cross-encoder reranker):本机对 15 条素材做 bge-reranker-v2-m3
# 重排要 ~7.5s,语义检索直接卡死;关掉后走 bi-encoder 向量 + RRF 融合,延迟降到 ~50ms,
# 对小规模素材库质量无损。需要重排时删掉这行即可(VITUAL_RERANK_MODEL 留空=按模型可用性自动)。
os.environ["VITUAL_RERANK_MODEL"] = "__none__"

import server

# Run the Materials Hub on an alternate port (8000 is occupied by another app
# on this machine). Keeps the in-repo /crawler console reachable.
server.PORT = 8200
server.main()
