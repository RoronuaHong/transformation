import os

# 关掉 stage-2 重排(cross-encoder reranker):本机对 15 条素材做 bge-reranker-v2-m3
# 重排要 ~7.5s,语义检索直接卡死;关掉后走 bi-encoder 向量 + RRF 融合,延迟降到 ~50ms,
# 对小规模素材库质量无损。需要重排时删掉这行即可(VITUAL_RERANK_MODEL 留空=按模型可用性自动)。
os.environ["VITUAL_RERANK_MODEL"] = "__none__"

# 允许读取用户登记的外部素材(桌面/下载里的原图)。不设则库外文件在 /api/file 被
# external_path_allowed 判为越权返回 403,前端卡片缩略图全裂。分号分隔,绝对路径。
os.environ.setdefault(
    "VITUAL_HUB_EXT_ROOTS",
    r"C:\Users\msi\Desktop;C:\Users\msi\Downloads",
)

import server

# Run the Materials Hub on :8200 (8000 is occupied by the FunUnion API on this
# machine). The crawler console now lives in transform (/[locale]/hub/crawler).
# PORT 已与 server.py 默认值一致;此处保留是为了显式带上 VITUAL_RERANK_MODEL 优化。
server.PORT = 8200
server.main()
