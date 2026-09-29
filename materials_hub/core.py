"""Materials Hub — 核心库(零依赖,Python 3.12)。

负责:目录初始化、文件分类、SHA-256 去重、命名规范化、SQLite 索引、检索。
所有路径相对 HUB 根,索引库为 index/hub.db。
"""
import os
import re
import json
import time
import pickle
import sqlite3
import shutil
import hashlib
import datetime
import subprocess
import threading

HUB = os.path.dirname(os.path.abspath(__file__))
MATERIALS = os.path.join(HUB, "materials")
INGEST = os.path.join(HUB, "ingest")
TRASH = os.path.join(HUB, "trash")
INDEX_DIR = os.path.join(HUB, "index")
OCR_DIR = os.path.join(INDEX_DIR, "ocr")       # 视频画面 OCR 文本 sidecar(派生,可重建)
SHOT_DIR = os.path.join(INDEX_DIR, "shots")    # 视频镜头索引 sidecar(派生数据,可随时重建)
PHASH_DIR = os.path.join(INDEX_DIR, "phash")   # dHash 感知哈希 sidecar(派生数据,可随时重建)
INDEX_DB = os.path.join(INDEX_DIR, "hub.db")
STATIC = os.path.join(HUB, "static")
THUMBS = os.path.join(INDEX_DIR, "thumbs")  # 视频封面缓存(派生数据,可随时删)

# 外部引用允许的根目录(防路径穿越/越权读取)。素材中心与 subtitle_pipeline 同处一个工作区,
# 默认只允许登记/读取该工作区内的文件;跨工作区的素材可用 VITUAL_HUB_EXT_ROOTS 追加(分号分隔,绝对路径)。
def _load_ext_roots():
    root = os.path.dirname(HUB)                      # 工作区根(素材中心的上一级目录)
    roots = [os.path.realpath(root)]
    extra = os.environ.get("VITUAL_HUB_EXT_ROOTS", "")
    for r in extra.split(";"):
        r = r.strip()
        if r:
            roots.append(os.path.realpath(r))
    return roots

EXT_ROOTS = _load_ext_roots()


def external_path_allowed(p):
    """外部引用路径白名单:realpath 后必须落在某个允许根内,否则视为越权。

    用途:① `ingest_external` 登记时拒绝工作区外的文件;
    ② `/api/file` 读取前再校验一次,即使索引库被写坏,也读不到工作区外的任何文件。"""
    try:
        p = os.path.realpath(p)
    except OSError:
        return False
    return any(p == r or p.startswith(r + os.sep) for r in EXT_ROOTS)

# 按扩展名分类。audio 单独成类便于检索。
KINDS = {
    "images": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".svg", ".tif", ".tiff", ".ico", ".heic"},
    "anim": {".gif"},
    "videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts"},
    "docs": {".pdf", ".md", ".txt", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
             ".csv", ".json", ".yaml", ".yml", ".html", ".epub", ".rtf"},
    "audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"},
    "subs": {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sbv"},
}


def init_hub():
    """创建标准目录结构并初始化数据库。幂等。"""
    for d in [MATERIALS, INGEST, TRASH, INDEX_DIR, OCR_DIR, SHOT_DIR, PHASH_DIR, STATIC]:
        os.makedirs(d, exist_ok=True)
    for k in list(KINDS) + ["other"]:
        os.makedirs(os.path.join(MATERIALS, k), exist_ok=True)
    _init_db()


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    for k, exts in KINDS.items():
        if ext in exts:
            return k
    return "other"


def compute_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sanitize_name(name):
    """命名规范化:非字母数字/中文/连字符统一为 -,折叠重复,截断。"""
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^\w一-鿿\-]+", "-", base.strip(), flags=re.UNICODE)
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        base = "file"
    return base[:80] + ext.lower()


# ---------- 数据库 ----------
def _con():
    return sqlite3.connect(INDEX_DB)


def _init_db():
    os.makedirs(INDEX_DIR, exist_ok=True)
    con = _con()
    con.execute(
        """CREATE TABLE IF NOT EXISTS materials(
            id TEXT PRIMARY KEY,
            kind TEXT,
            ext TEXT,
            name TEXT,
            rel_path TEXT,
            size INTEGER,
            sha256 TEXT,
            tags TEXT,
            description TEXT,
            source TEXT,
            orig_name TEXT,
            created_at TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_sha ON materials(sha256)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_kind ON materials(kind)")
    # 语义索引:向量以 float32 BLOB 存库,sig 用于检测文档文本变化(增量重建)
    con.execute(
        """CREATE TABLE IF NOT EXISTS embeddings(
            mid TEXT PRIMARY KEY,
            model TEXT,
            dim INTEGER,
            sig TEXT,
            vec BLOB,
            updated_at TEXT
        )"""
    )
    for col, ddl in [("location", "TEXT DEFAULT 'internal'"),
                     ("external_path", "TEXT DEFAULT ''"),
                     ("ai_tags", "TEXT DEFAULT ''")]:
        try:
            con.execute(f"ALTER TABLE materials ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    # 写操作审计(对齐 2026 MCP 安全实践「谁在何时改了什么」,见最佳实践 §17.3):
    # 只记录显式变更(标签/描述/删除/打标/OCR),bridge 批量登记不记(幂等同步非人为变更)。
    con.execute(
        """CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            actor TEXT,
            action TEXT,
            target_id TEXT,
            detail TEXT
        )"""
    )
    con.commit()
    con.close()


# ---------- 写操作审计 ----------
def log_history(actor, action, target_id="", detail=""):
    """记录一条写操作审计。任何失败都不抛错(审计不该阻塞业务)。"""
    try:
        con = _con()
        con.execute(
            "INSERT INTO history(ts,actor,action,target_id,detail) VALUES(?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             actor or "app", action, target_id or "", (detail or "")[:300]),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def get_history(limit=50, target_id=""):
    """按时间倒序返回写操作审计记录(可选按素材过滤)。"""
    con = _con()
    con.row_factory = sqlite3.Row
    if target_id:
        rows = con.execute(
            "SELECT * FROM history WHERE target_id=? ORDER BY id DESC LIMIT ?",
            (target_id, int(limit)),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def add_material(**kw):
    con = _con()
    con.execute(
        """INSERT OR REPLACE INTO materials
           (id,kind,ext,name,rel_path,size,sha256,tags,description,source,orig_name,created_at)
           VALUES(:id,:kind,:ext,:name,:rel_path,:size,:sha256,:tags,:description,:source,:orig_name,:created_at)""",
        kw,
    )
    con.commit()
    con.close()


def get_material(mid):
    con = _con()
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    con.close()
    return dict(r) if r else None


def _update_material(mid, **fields):
    """就地更新素材字段(描述/标签等),按 id 定位。"""
    if not fields:
        return
    con = _con()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE materials SET {cols} WHERE id=?", list(fields.values()) + [mid])
    con.commit()
    con.close()


def all_materials():
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM materials ORDER BY created_at DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def _tokens(s):
    """零依赖分词:英文/数字按词,中文按单字+bigram。返回 token 集合。"""
    s = (s or "").lower()
    toks = set()
    for m in re.findall(r"[a-z0-9]+", s):
        toks.add(m)
    cn = re.findall(r"[一-鿿]", s)
    for w in cn:
        toks.add("c:" + w)
    for i in range(len(cn) - 1):
        toks.add("b:" + cn[i] + cn[i + 1])
    return toks


# 字段权重:名称最重,其次标签,再次描述,路径最轻。
_FIELD_WEIGHT = {"name": 3.0, "tags": 2.5, "description": 1.5, "rel_path": 1.0}


def _path_tokens(p):
    """路径分词,只取末 3 段。

    必要性(实测):外部引用的 rel_path 是**绝对路径**,里面含工作区/项目名
    (如 `…\\Vitual\\subtitle_pipeline\\…`),于是查"字幕/subtitle"时 **341 条全部命中**,
    命中数直接失去意义。末 3 段保留了阶段目录(media/dehardsub/deblur/mosaic)与 job id,
    去掉的是工作区前缀。"""
    parts = [x for x in re.split(r"[\\/]+", p or "") if x and x != ":"]
    return _tokens(" ".join(parts[-3:]))


def _tok_w(t):
    """token 类型权重:英文/数字意图词全权;中文 bigram 噪声大降权;中文单字更弱。
    必要性:本库元数据是英文,中文问句真正起作用的是同义词表扩出的英文词;
    而中文描述(如 bilibili 视频标题)与查询中文 bigram 任意重叠会造成大量误召回、
    把真正命中的英文素材挤下前排。降权中文后,英文意图词重新主导排序。"""
    if t[:2] == "b:":
        return 0.5
    if t[:2] == "c:":
        return 0.3
    return 1.0


def _score(q_tokens, m):
    """加权近似打分:query token 与素材各字段 token 重叠累计(按类型加权,见 _tok_w)。

    含一层**松匹配**:英文/数字 token 长度 ≥5 时,互为子串也算命中(权重 0.6)。
    实测必要性:数据里是 `demosaic`,查 `mosaic` 时严格分词匹配不上;
    `dehardsub` 同理。松匹配只在严格匹配为 0 时才尝试,避免误召回放大。"""
    sc = 0.0
    for field, w in _FIELD_WEIGHT.items():
        dt = _path_tokens(m.get(field, "")) if field == "rel_path" else _tokens(m.get(field, ""))
        ov = q_tokens & dt
        if ov:
            sc += w * sum(_tok_w(t) for t in ov)
            continue
        loose = 0.0
        for qt in q_tokens:
            if len(qt) < 5 or qt[:2] in ("c:", "b:"):
                continue
            if any(qt in d for d in dt):
                loose += _tok_w(qt) * 0.6
        if loose:
            sc += w * loose
    return sc


def query_materials(q="", kind="", tag=""):
    """按筛选条件取出并排好序的全部结果(不分页)。
    q 为空时按时间倒序(等同浏览全部);有 q 时按加权得分倒序。"""
    con = _con()
    con.row_factory = sqlite3.Row
    sql = "SELECT * FROM materials WHERE 1=1"
    params = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if tag:
        sql += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    rows = con.execute(sql, params).fetchall()
    con.close()
    rows = [dict(r) for r in rows]

    if not q:
        rows.sort(key=lambda m: m["created_at"], reverse=True)
        return rows

    qt = _tokens(q)
    if not qt:
        return rows

    scored = [(_score(qt, m), m) for m in rows]
    scored = [(s, m) for s, m in scored if s > 0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored]


def _ranked(q="", kind="", tag="", mode="auto"):
    """统一检索入口:返回 (结果列表, 是否用了语义)。
    没有 q 时就是 kind/tag 过滤 + 时间倒序(与旧行为一致)。"""
    if not q:
        return query_materials("", kind, tag), False
    q2 = expand_query(q)                 # 中文问句补上语料英文词汇
    lex = query_materials(q2, kind, tag)
    if mode == "lexical" or not embed_probe()["ok"]:
        return lex, False
    # 语义检索在「kind/tag 过滤后的整个语料」上排名,才能召回词法完全没命中的条目
    base = query_materials("", kind, tag)
    return semantic_rank(q2, base, lex)


def search(q="", kind="", tag="", limit=None, offset=0, mode="auto", sort=""):
    """检索:自然语言问句 → 排序后返回,支持分页。
    mode="auto"     有 embedding 模型且素材已建向量 → 稠密+词法混合;
                    否则纯词法加权(与旧行为一致)
    mode="lexical"  强制词法;mode="semantic" 强制语义(无向量时自动回退词法)
    limit=None 表示不分页;offset 在排序之后生效(全局偏移,非页内)。
    sort=""         默认相关性/时间序;q 存在时=相关性,无 q=时间倒序。
                    显式指定 "newest"/"oldest"/"name"/"size" 时覆盖相关性排序
                    (DAM 面板的排序控件;在分页之前生效,全库级排序)。"""
    ck = (q, kind, tag, limit, offset, mode, sort)
    if os.environ.get("VITUAL_CACHE_SEARCH"):
        if ck in _SEARCH_CACHE:
            return _SEARCH_CACHE[ck]
    rows, _ = _ranked(q, kind, tag, mode)
    if sort in ("newest", "oldest", "name", "size"):
        if sort == "name":
            rows = sorted(rows, key=lambda m: (m.get("name") or "").lower())
        elif sort == "size":
            rows = sorted(rows, key=lambda m: m.get("size") or 0, reverse=True)
        else:
            key = lambda m: m.get("created_at") or ""        # noqa: E731
            rows = sorted(rows, key=key, reverse=(sort == "newest"))
    if offset > 0 or limit is not None:
        end = None if limit is None else offset + limit
        rows = rows[offset:end]
    if os.environ.get("VITUAL_CACHE_SEARCH"):
        if len(_SEARCH_CACHE) < 2000:        # 简易内存缓存(同查询重复率不低,省重算)
            _SEARCH_CACHE[ck] = rows
            _cache_persist()                 # 开启持久化时落盘
    return rows


# ---------- 查询结果缓存(可选,默认关;§15.2 生产 RAG 的语义缓存思路本地版) ----------
_CACHE_PATH = os.path.join(INDEX_DIR, "search_cache.pkl")


def _cache_load():
    if not os.environ.get("VITUAL_CACHE_PERSIST"):
        return {}
    try:
        with open(_CACHE_PATH, "rb") as f:
            return pickle.load(f)
    except Exception:
        return {}


def _cache_persist():
    if not os.environ.get("VITUAL_CACHE_PERSIST"):
        return
    try:
        with open(_CACHE_PATH, "wb") as f:
            pickle.dump(dict(_SEARCH_CACHE), f)
    except Exception:
        pass


_SEARCH_CACHE = _cache_load() if os.environ.get("VITUAL_CACHE_PERSIST") else {}


# 长文档父子分块:除库内 description 外,这些扩展名的「关联文本文件」也参与分块检索
# (对应 §15.2:把 .md/笔记/字幕文本本身也做成可检索的子块,而非只看元数据)。
_TEXT_EXT = (".md", ".txt", ".srt", ".ass", ".vtt", ".json", ".csv", ".py",
            ".yaml", ".yml", ".toml", ".log")


def _material_text(m):
    """素材可检索的全文:库内 description + 关联文本文件内容(若 external_path/rel_path
    指向可读文本文件)+ 视频画面 OCR sidecar(index/ocr/<id>.txt,若已生成)。
    媒体文件本身不读,只取文本类。只读、不写、不越权。"""
    parts = [(m.get("description") or "").strip()]
    p = m.get("external_path") or ""
    if not p:
        rp = m.get("rel_path", "")
        if rp and not os.path.isabs(rp):
            p = os.path.join(HUB, rp)
    if p and p.lower().endswith(_TEXT_EXT) and os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                parts.append(f.read())
        except Exception:
            pass
    sc = ocr_sidecar_path(m.get("id", ""))
    if sc and os.path.isfile(sc):
        try:
            with open(sc, "r", encoding="utf-8", errors="ignore") as f:
                parts.append(f.read())
        except Exception:
            pass
    return "\n".join(x for x in parts if x)


def _split_chunks(text, size=140, overlap=30):
    """把长文本切成 ~size 字符的子块(父子分块思想:子块精检、父块=整素材)。
    先按段落/换行切,段落超长再按句末标点/空格切;overlap 让边界语义不丢。"""
    if not text or not text.strip():
        return []
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks, buf = [], ""
    def flush():
        nonlocal buf
        if buf:
            chunks.append(buf); buf = ""
    for p in paras:
        if len(buf) + len(p) <= size:
            buf = (buf + "\n" + p).strip()
            continue
        flush()
        if len(p) <= size:
            chunks.append(p); continue
        for piece in re.split(r"(?<=[。！？!?；;])", p):
            piece = piece.strip()
            if not piece:
                continue
            if len(piece) <= size:
                chunks.append(piece)
            else:
                step = max(1, size - overlap)
                for i in range(0, len(piece), step):
                    chunks.append(piece[i:i + size])
    flush()
    return chunks


def chunk_score(qt, m):
    """对单个素材做父子分块打分:把全文(description+关联文本文件)切成子块,
    词法匹配每个子块,返回最佳子块得分。供 chunk_search 复用。"""
    best = 0.0
    for c in _split_chunks(_material_text(m)):
        sc = _score(qt, {"description": c})        # 复用加权打分(只看子块文本)
        if sc > best:
            best = sc
    return best


def chunk_search(q, limit=10, kind="", tag=""):
    """长文档父子分块检索(对应 §15.2 生产 RAG 的 Parent-Child 分块):
    对每个素材的全文(description + 关联文本文件,见 `_material_text`)做子块切分,
    词法匹配子块,返回命中的素材(按最佳子块得分降序)。适合在长笔记/字幕文本里
    按段落精准命中,而非整段模糊匹配。纯只读、零副作用;由 MCP `chunk_search_materials` 暴露。"""
    qt = _tokens(q)
    if not qt:
        return []
    con = _con(); con.row_factory = sqlite3.Row
    sql = ("SELECT id,name,description,tags,kind,rel_path,ai_tags,size,ext,location,"
           "external_path FROM materials WHERE 1=1")
    params = []
    if kind:
        sql += " AND kind=?"; params.append(kind)
    if tag:
        sql += " AND tags LIKE ?"; params.append(f"%{tag}%")
    rows = con.execute(sql, params).fetchall(); con.close()
    hits = []
    for m in rows:
        m = dict(m)                     # sqlite3.Row 无 .get(),统一转 dict 供 _material_text/_score 使用
        s = chunk_score(qt, m)
        if s > 0:
            hits.append((s, m))
    hits.sort(key=lambda x: -x[0])
    return [dict(r) for _, r in hits[:limit]]


def count_materials(q="", kind="", tag="", mode="auto"):
    """当前条件下的命中总数(配合分页使用;语义模式下与排序结果集一致)。"""
    return len(_ranked(q, kind, tag, mode)[0])


# ---------- 语义检索(本地 ollama embedding,零依赖) ----------
_EMBED_CACHE = {"probed": False, "model": "", "url": "", "ok": False, "err": ""}


def _embed_url():
    return os.environ.get("VITUAL_EMBED_URL", "http://127.0.0.1:11434").rstrip("/")


def _http_json(url, payload=None, timeout=90):
    import urllib.request
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- 可选 stage-2 重排(cross-encoder reranker) ----------
def _ollama_rerank(q, docs, model=None):
    """可选 stage-2 重排:本地 ollama cross-encoder reranker(如 bge-reranker-v2-m3)。

    调用 ollama `/api/rerank`,返回按相关性降序的文档下标列表;任何异常(未配模型 /
    模型不存在 / 服务没起)都返回 None —— 调用方据此跳过重排,降级为原 RRF 融合结果。
    这是生产 RAG 的标准二阶段:bi-encoder 召回 Top-N → cross-encoder 精排 Top-K。
    默认不启用,需设 VITUAL_RERANK_MODEL 才生效,零模型依赖下对旧行为零影响。"""
    model = model or os.environ.get("VITUAL_RERANK_MODEL", "").strip()
    if not model or not docs:
        return None
    try:
        r = _http_json(_embed_url() + "/api/rerank",
                       {"model": model, "query": q, "documents": docs,
                        "top_n": len(docs)}, timeout=60)
        order = sorted(r.get("results", []), key=lambda x: -x.get("relevance_score", 0))
        return [x["index"] for x in order]
    except Exception:
        return None


def _lexical_rerank(q, docs, model=None):
    """内置离线 reranker(无需 ollama/cross-encoder):按「查询词在文档中的加权命中数」对候选重排。

    这是 stage-2 重排管线的**离线可用弱基线**——用于验证二阶段架构本身、以及在拿不到
    cross-encoder 时给出一个可跑的 A/B 对照组。它本质是「更强的词法重排」,无法像
    cross-encoder 那样建模查询-文档交互,故真目标仍是设 VITUAL_RERANK_MODEL=bge-reranker-v2-m3
    走 `_ollama_rerank`。任何异常都返回 None(降级为原 RRF 融合)。"""
    qt = _tokens(q)
    if not qt or not docs:
        return None
    scored = []
    for d in docs:
        dt = set(_tokens(d))
        scored.append(sum(_tok_w(t) for t in qt if t in dt))
    return sorted(range(len(docs)), key=lambda i: -scored[i])


def embed_probe(refresh=False):
    """探测本地 embedding 能力:ollama 是否有 embedding 模型。
    结果缓存(_EMBED_CACHE),避免每次检索都探测。"""
    if _EMBED_CACHE["probed"] and not refresh:
        return _EMBED_CACHE
    _EMBED_CACHE.update(probed=True, ok=False, model="", url=_embed_url(), err="")
    want = os.environ.get("VITUAL_EMBED_MODEL", "").strip()
    try:
        tags = _http_json(_embed_url() + "/api/tags", timeout=8)
    except Exception as e:              # 服务没起 / 端口不通 → 明确记下原因
        _EMBED_CACHE["err"] = f"{type(e).__name__}: {e}"
        return _EMBED_CACHE
    cands = []
    for m in tags.get("models", []):
        caps = m.get("capabilities") or []
        if "embedding" in caps or (m.get("name", "").split(":")[0] in ("nomic-embed-text", "bge-m3", "mxbai-embed-large")):
            cands.append(m["name"])
    if want:
        # 显式指定优先;名称可省略 tag(ollama 会补 :latest)
        hit = next((c for c in cands if c == want or c.split(":")[0] == want), None)
        if hit:
            cands = [hit] + [c for c in cands if c != hit]
        else:
            cands = [want] + cands
    elif cands:
        # 未显式指定时,优先多语模型(bge-m3 对本库中文查询更友好),
        # 否则退回 nomic 等英文单语模型。语义在此库只作"词法未命中时的召回兜底"。
        pref = [c for c in cands if c.split(":")[0] == "bge-m3"]
        if pref:
            cands = pref + [c for c in cands if c not in pref]
    if cands:
        _EMBED_CACHE.update(ok=True, model=cands[0])
    return _EMBED_CACHE


def _ollama_exe():
    """定位 ollama 可执行文件:环境变量 → PATH → Windows 默认安装位。没有则 None。"""
    import shutil
    env = os.environ.get("VITUAL_OLLAMA", "").strip()
    if env and os.path.isfile(env):
        return env
    got = shutil.which("ollama")
    if got:
        return got
    if os.name == "nt":
        cand = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
        if os.path.isfile(cand):
            return cand
        return None
    p = os.path.expanduser("~/.local/bin/ollama")
    return p if os.path.isfile(p) else None


def ensure_ollama(timeout=15):
    """语义检索依赖本地 ollama;服务没起时尽力自动拉起(实测:重启电脑后模型服务
    不会自己回来,语义检索会静默降级成词法)。成功/本就在线 → ok=True;
    失败不抛错,检索层会自动回退词法,面板状态栏会如实显示「未启用」。"""
    if embed_probe(refresh=True)["ok"]:
        return embed_probe()
    exe = _ollama_exe()
    if not exe:
        return embed_probe()
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        return embed_probe()
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        _t.sleep(0.5)
        if embed_probe(refresh=True)["ok"]:
            break
    return embed_probe()


# ---------- 自动打标(本地 LLM,零外部依赖) ----------
def chat_models():
    """列出 ollama 中具备「生成」能力的模型(排除仅 embedding 的)。无则空列表。

    用途:自动打标走本地 LLM 生成描述/标签;离线、无 API key。没有 chat 模型时
    调用方应优雅跳过(见 auto_tag_material 的 skipped 状态),不抛错、不阻断检索。"""
    try:
        tags = _http_json(_embed_url() + "/api/tags", timeout=8)
    except Exception:
        return []
    out = []
    for m in tags.get("models", []):
        caps = m.get("capabilities") or []
        if "embedding" in caps:          # 只做向量的模型不能生成文本
            continue
        out.append(m["name"])
    return out


def _safe_json(s):
    """尽力从模型输出里解析出第一个 JSON 对象(去 ```围栏/控制符/尾逗号)。"""
    if not s:
        return None
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _build_tag_prompt(m):
    p = m.get("external_path") or m.get("rel_path") or ""
    dirs = [d for d in os.path.dirname(p).replace("\\", "/").split("/") if d][-3:]
    return (
        "你是多媒体素材库的管理员。请为一则素材提炼元数据。\n"
        f"文件名: {m.get('name', '')}\n"
        f"类型: {m.get('kind', '')}\n"
        f"已有标签: {m.get('tags', '')}\n"
        f"所在目录: {' / '.join(dirs)}\n"
        f"已有描述: {(m.get('description') or '').strip()}\n\n"
        "用一行 JSON 回复(不要解释、不要 markdown 围栏):\n"
        '{"desc": "<中文短描述,≤40字,说明这是什么/用于什么>", '
        '"tags": ["<英文小写标签1>", ...最多8个]}\n'
        "标签只用素材库已有的英文词(dehardsub/deblur/codeformer/segment/bilibili/"
        "subs/out/fixed...),不要造中文标签,也不要重复已有标签。")


# ---------- LLM 标签归一化护栏 ----------
# 防止 LLM 输出通用/噪声标签污染 tags(词法检索打分源)。
# 见 2026-09-27/09-28 复盘: video/媒体/场景/人物 等通用标签会沦为词法噪声,拖垮 P@5。
_LLM_TAG_STOP = {
    # 语言/通用空标签(中英文)
    "video","videos","media","multimedia","footage","clip","clips","scene","scenes",
    "content","raw","person","people","character","characters","render","renders",
    "material","materials","asset","assets","sample","samples","file","files",
    "image","images","photo","photos","picture","pictures","audio","movie","movies",
    "test","testing","demo","example","unknown","none","null","untitled","new","old",
    "素材","视频","媒体","片段","场景","内容","人物","画面","渲染","测试","笔记",
    "说明","预览","其他","无","空","默认","通用","杂","资料","文件","图片","照片",
    # 通用动词/状态词(对检索无区分度,纯噪声)
    "type","fix","fixed","fast","verify","verified","review","check","result",
    "output","processed","final","clean","cleaned","enhanced","upscaled","restored",
    "model","ai","tool","auto","batch","good","bad","best","high","low","quality",
}
_LLM_TAG_MAX = 6          # 单条最多保留的 LLM 标签数
_LLM_TAG_MAX_LEN = 20     # 单标签最大长度(超长视为噪声/句子)
AUTOTAG_BACKUP = os.path.join(INDEX_DIR, "autotag_backup.json")


def _normalize_llm_tags(raw):
    """把 LLM 返回的任意标签规整为安全、可用的 token。

    - 小写、下划线/空格转连字符,仅保留 [a-z0-9 中文 -]
    - 丢弃停用词/通用噪声标签、type:/sp/job: 等系统溯源标签、过长(>20)与超量(>6)标签
    - 去重保序"""
    out, seen = [], set()
    for t in (raw or []):
        if not t:
            continue
        s = str(t).strip().lower().replace("_", "-").replace(" ", "-")
        s = "".join(ch for ch in s
                    if ch == "-" or (ch.isascii() and ch.isalnum())
                    or ("\u4e00" <= ch <= "\u9fff"))
        if not s or s in seen or s in _LLM_TAG_STOP:
            continue
        # 拦掉系统溯源标签(sp / type / job 前缀)。注意上面的字符过滤会剥掉冒号,
        # 故必须用无冒号前缀匹配(否则 job:fetch→jobfetch 会漏过)。
        if s == "sp" or s.startswith(("type", "job")):
            continue
        if len(s) > _LLM_TAG_MAX_LEN:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= _LLM_TAG_MAX:
            break
    return out


def auto_tag_material(m, dry=False, model=None, models=None):
    """用本地 LLM 为单条素材生成描述+英文标签。

    返回 dict: status ∈ ok|dry|skipped|error|parse_error。
    - 无 chat 模型 → skipped(不调网络、不抛错)
    - dry=True 只返回模型建议,不写库
    - 写库策略:描述仅在原文为空或过短(<20字)时覆盖;标签经归一化护栏后追加去重,
      保留 sp|/type: 等既有溯源标签不动。"""
    if models is None:
        models = chat_models()
    if not models:
        return {"id": m["id"], "status": "skipped", "reason": "no_chat_model"}
    model = model or models[0]
    try:
        r = _http_json(_embed_url() + "/api/generate", timeout=90,
                       payload={"model": model, "prompt": _build_tag_prompt(m),
                                "format": "json", "stream": False})
    except Exception as e:
        return {"id": m["id"], "status": "error", "reason": f"{type(e).__name__}: {e}"}
    data = _safe_json((r.get("response") or "").strip())
    if not data:
        return {"id": m["id"], "status": "parse_error", "raw": (r.get("response") or "")[:200]}
    desc = (data.get("desc") or "").strip()
    tags = _normalize_llm_tags(data.get("tags"))
    if dry:
        return {"id": m["id"], "status": "dry", "desc": desc, "tags": tags}
    cur = get_material(m["id"])
    cur_desc = (cur.get("description") or "").strip()
    new_desc = desc if (not cur_desc or len(cur_desc) < 20) else cur_desc
    # LLM 标签写入独立列 ai_tags(供 Agent/MCP 读取),绝不污染词法打分用的 tags。
    cur_ai = [t.strip() for t in (cur.get("ai_tags") or "").split(",") if t.strip()]
    added = []
    for t in tags:
        if t not in cur_ai:
            cur_ai.append(t)
            added.append(t)
    _update_material(m["id"], description=new_desc, ai_tags=",".join(cur_ai))
    log_history("app", "auto_tag", m["id"],
                f"ai_tags+={added!r}; desc={'set' if new_desc == desc and desc else 'kept'}")
    return {"id": m["id"], "status": "ok", "desc": new_desc, "added_tags": added}


def auto_tag_all(limit=0, dry=False, model=None, backup=True):
    """批量为全部素材打标。没有 chat 模型时整批 skipped(秒回,不调网络)。

    写库前自动快照各素材 (tags, ai_tags) 到 AUTOTAG_BACKUP(仅当备份不存在时,
    避免覆盖更早基线),可用 autotag_undo() 回滚本次写入。dry 模式不写库、不备份。"""
    models = chat_models()
    ms = all_materials()
    if limit:
        ms = ms[:limit]
    if not dry and backup and models and not os.path.exists(AUTOTAG_BACKUP):
        try:
            snap = {m["id"]: {
                "tags": (get_material(m["id"]) or {}).get("tags", "") or "",
                "ai_tags": (get_material(m["id"]) or {}).get("ai_tags", "") or "",
            } for m in ms}
            with open(AUTOTAG_BACKUP, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
        except (OSError, ValueError):
            pass
    return [auto_tag_material(m, dry=dry, model=model, models=models) for m in ms]


def autotag_undo():
    """回滚最近一次 auto_tag_all 写入的标签:从 AUTOTAG_BACKUP 恢复各素材
    (tags, ai_tags),然后删除备份文件。非破坏式,不动描述;没有备份时返回 0。"""
    if not os.path.exists(AUTOTAG_BACKUP):
        return 0
    try:
        with open(AUTOTAG_BACKUP, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    for mid, rec in snap.items():
        cur = get_material(mid)
        if not cur:
            continue
        if isinstance(rec, dict):
            bak_tags = rec.get("tags", "") or ""
            bak_ai = rec.get("ai_tags", "") or ""
        else:  # 兼容旧格式(仅 tags 字符串)
            bak_tags, bak_ai = (rec or ""), ""
        changed = False
        if (cur.get("tags") or "") != bak_tags:
            changed = True
        if (cur.get("ai_tags") or "") != bak_ai:
            changed = True
        if changed:
            _update_material(mid, tags=bak_tags, ai_tags=bak_ai)
            n += 1
    try:
        os.remove(AUTOTAG_BACKUP)
    except OSError:
        pass
    return n


# ---------- 规则打标(离线、确定性、零模型) ----------
# 关键词 → 英文标签。只加「有区分度、type: 未覆盖」的语义标签;
# 不复制 type:render/media/test/notes 等(会沦为词法打分噪声,见 2026-09-27 回归复盘),
# 且 subs 只用真实字幕扩展名(srt/ass/vtt/subs),不用 subtitle/caption(会命中 pipeline 目录名)。
_RULE_TAGS = [
    (("dehardsub", "hardsub"), "dehardsub"),
    (("deblur", "clean", "sharpen"), "deblur"),
    (("segment", "clip", "cut", "slice"), "segment"),
    (("srt", "ass", "vtt", "subs"), "subs"),
    (("cmp", "compare", "对比"), "cmp"),
    (("codeformer", "gfpgan", "face", "restore", "fixed", "修复"), "facefix"),
    (("benchmark", "bench", "基准"), "benchmark"),
    (("bilibili", "b站"), "bilibili"),
    (("combine", "merge", "合成", "合并"), "combine"),
    (("watermark", "logo", "水印", "台标"), "watermark"),
    (("out", "final", "成品", "修好"), "out"),
]

# 主标签 → 中文短描述模板(描述为空时填充)。
_RULE_DESC = {
    "dehardsub": "去字幕、保留背景的素材",
    "deblur": "画面去模糊/增强清晰的素材",
    "segment": "按时间切出的视频片段",
    "subs": "字幕文本(外挂字幕)文件",
    "cmp": "渲染/处理前后对比结果",
    "facefix": "人脸修复(超分/还原)素材",
    "benchmark": "基准测试视频",
    "bilibili": "B站下载的素材",
    "combine": "合成/合并后的素材",
    "watermark": "含水印/台标的素材",
    "render": "渲染结果",
    "out": "处理完成的成品",
    "probe": "抽帧预览图",
    "test": "测试用素材",
    "media": "媒体素材",
    "notes": "笔记/说明文档",
}


def _rule_scan(m):
    # 只扫素材自身属性(name/tags/description/kind) + 路径 basename,
    # 不扫整条目录树——否则 subtitle_pipeline/mode-renders/outputs 等通用文件夹名
    # 会被 subs/out/media 等关键词误命中,造成全员误标。type: 已表达的 render/media 不重复加。
    parts = [str(m.get(k, "")) for k in ("name", "tags", "description", "kind")]
    p = m.get("external_path") or m.get("rel_path") or ""
    if p:
        parts.append(os.path.basename(p))
    text = " ".join(parts).lower()
    return [tag for keys, tag in _RULE_TAGS if any(k in text for k in keys)]


def rule_tag_material(m, dry=False):
    """离线、确定性的规则打标:从文件名/路径/标签/描述抽英文标签 + 生成中文短描述。
    不依赖任何模型;保留既有标签、仅追加新识别项,描述仅在为空时填充(非破坏式)。"""
    cur = get_material(m["id"])
    cur_tags = [t.strip() for t in (cur.get("tags") or "").split(",") if t.strip()]
    found = _rule_scan(m)
    added = [t for t in found if t not in cur_tags]
    new_tags = cur_tags + added
    cur_desc = (cur.get("description") or "").strip()
    if cur_desc:
        new_desc = cur_desc
    else:
        new_desc = _RULE_DESC.get(found[0], "媒体素材") if found else "媒体素材"
    if dry:
        return {"id": m["id"], "status": "dry", "tags": new_tags, "added": added, "desc": new_desc}
    _update_material(m["id"], description=new_desc, tags=",".join(new_tags))
    log_history("app", "rule_tag", m["id"], f"tags+={added!r}")
    return {"id": m["id"], "status": "ok", "added_tags": added, "desc": new_desc}


def rule_tag_all(limit=0, dry=False):
    """批量规则打标。非破坏式:只追加标签、仅在描述为空时填充。"""
    ms = all_materials()
    if limit:
        ms = ms[:limit]
    return [rule_tag_material(m, dry=dry) for m in ms]


# 规则打标曾写入的全部标签(含早期较宽的 test/media/render/probe/notes);
# 回滚时剥离这些,但保留 bridge 合法写入的 bilibili 等。
_RULE_ADDED_TAGS = {"dehardsub", "deblur", "segment", "subs", "cmp", "facefix",
                    "benchmark", "bilibili", "combine", "watermark", "out",
                    "test", "media", "render", "probe", "notes"}


def rule_tag_cleanup():
    """回滚规则打标:剥离本模块写入的标签(保留 bridge 合法写入的 bilibili 等)。
    非破坏式,不改描述。返回移除的标签总数。"""
    removed = 0
    for m in all_materials():
        cur = get_material(m["id"])
        tags = [t.strip() for t in (cur.get("tags") or "").split(",") if t.strip()]
        new = [t for t in tags if t not in _RULE_ADDED_TAGS or t == "bilibili"]
        if len(new) != len(tags):
            _update_material(m["id"], tags=",".join(new))
            removed += len(tags) - len(new)
    return removed


def embed_texts(texts, model=None):
    """批量取 embedding。失败返回 None(调用方回退词法检索)。
    优先 /api/embed(批量),不支持则退回 /api/embeddings(逐条)。"""
    if not texts:
        return []
    info = embed_probe()
    if not info["ok"]:
        return None
    model = model or info["model"]
    url = _embed_url()
    try:
        r = _http_json(url + "/api/embed", {"model": model, "input": list(texts)})
        if isinstance(r.get("embeddings"), list) and len(r["embeddings"]) == len(texts):
            return r["embeddings"]
    except Exception:
        pass
    out = []
    for t in texts:                      # 老版本 ollama:逐条
        try:
            r = _http_json(url + "/api/embeddings", {"model": model, "prompt": t})
            out.append(r["embedding"])
        except Exception:
            return None
    return out if len(out) == len(texts) else None


def doc_text(m):
    """把一条素材拼成用于 embedding 的文档文本。

    经验要点(实测:库内两两余弦均值会从 0.76 降到更可分的水平):
    1. **去掉模板词** —— `sp` 每条都有,只会把所有向量拉向同一方向;
       `type:`/`lang:`/`job:` 这类维度标签保留(有区分度)。
    2. **文件名/目录名按下划线连字符拆词** —— `clean_lama_writing.mp4` →
       `clean lama writing`,模型才能对上 lama/clean 这些词。
    3. **外部引用的目录段含阶段语义**(dehardsub/deblur/mosaic/probe),取末几段。
    4. nomic-embed-text 要求文档加 `search_document:` 前缀(查询用 `search_query:`)。
    """
    stem, ext = os.path.splitext(m.get("name", ""))
    words = re.sub(r"[_\-.]+", " ", stem)
    if ext:
        words += " " + ext.lstrip(".")      # 扩展名也是语义(srt/ass/mp4/png),别丢
    p = m.get("external_path") or m.get("rel_path") or ""
    dirs = [d for d in os.path.dirname(p).replace("\\", "/").split("/") if d]
    stage = re.sub(r"[_\-.]+", " ", " ".join(dirs[-4:]))   # 同样避开工作区前缀(见 _path_tokens)
    tags = [t.strip() for t in (m.get("tags") or "").split(",") if t.strip() and t.strip() != "sp"]
    parts = [words, " ".join(tags), m.get("description", ""), m.get("kind", ""), stage]
    return "search_document: " + " | ".join(x for x in parts if x)


def _text_sig(t):
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


def _vec_to_blob(v):
    import array
    a = array.array("f", v)
    return a.tobytes()


def _blob_to_vec(b):
    import array
    a = array.array("f")
    a.frombytes(b)
    return a.tolist()


def build_embeddings(force=False, limit=0, progress=None):
    """增量构建/刷新语义索引。返回统计 dict。无 ollama embedding 模型时返回 available=False。"""
    info = embed_probe(refresh=True)
    if not info["ok"]:
        return {"available": False, "model": "", "total": 0, "embedded": 0, "skipped": 0}
    model = info["model"]
    ms = all_materials()
    con = _con()
    con.row_factory = sqlite3.Row
    have = {r["mid"]: (r["model"], r["sig"]) for r in con.execute(
        "SELECT mid, model, sig FROM embeddings").fetchall()}
    con.close()
    todo = []
    for m in ms:
        txt = doc_text(m)
        sig = _text_sig(txt)
        if not force and have.get(m["id"]) == (model, sig):
            continue
        todo.append((m["id"], txt, sig))
    total = len(ms)
    skipped = total - len(todo)
    if limit:
        todo = todo[:limit]
    done = 0
    for i in range(0, len(todo), 16):
        chunk = todo[i:i + 16]
        vecs = embed_texts([t for _, t, _ in chunk], model=model)
        if vecs is None:
            break
        con = _con()
        for (mid, _t, sig), v in zip(chunk, vecs):
            con.execute("INSERT OR REPLACE INTO embeddings(mid,model,dim,sig,vec,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (mid, model, len(v), sig, _vec_to_blob(v),
                         datetime.datetime.now().isoformat(timespec="seconds")))
        con.commit()
        con.close()
        done += len(chunk)
        if progress:
            progress(done, len(todo))
    return {"available": True, "model": model, "total": total,
            "embedded": done, "skipped": skipped, "pending": len(todo) - done}


def embed_status():
    """语义索引状态:模型 / 已索引数 / 覆盖率。"""
    info = embed_probe()
    con = _con()
    con.row_factory = sqlite3.Row
    n = con.execute("SELECT COUNT(*) c FROM embeddings WHERE model=?", (info["model"],)).fetchone()["c"]
    total = con.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"]
    con.close()
    return {"available": info["ok"], "model": info["model"], "url": info["url"],
            "embedded": n, "total": total, "err": info.get("err", ""),
            "coverage": round(n / total, 3) if total else 0.0}


def _normalize(v):
    import math
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def load_vectors(mids, model):
    if not mids:
        return {}
    out = {}
    con = _con()
    con.row_factory = sqlite3.Row
    q = ("SELECT mid, vec FROM embeddings WHERE model=? AND mid IN (%s)"
         % ",".join("?" * len(mids)))
    for r in con.execute(q, [model] + list(mids)).fetchall():
        out[r["mid"]] = _blob_to_vec(r["vec"])
    con.close()
    return out


def semantic_rank(q, base_rows, lexical_rows=None):
    """稠密 + 词法混合排序,返回 (排序后的结果, 是否真的用了语义)。

    两个关键工程处理:
    * **去均值居中**(mean-centering):句向量普遍各向异性(本库实测两两余弦均值 0.76),
      直接算余弦时"所有东西都像所有东西"。减去语料均值向量后相关/无关才拉得开。
    * **RRF 融合**稠密排名与词法排名,规避两套分数的量纲差异;
      且语料里没做向量的条目仍靠词法参与,不会凭空消失。
    结果集 = 稠密相似度过线的 ∪ 词法命中的(过线阈值可用 VITUAL_EMBED_MIN_COS 调)。
    """
    info = embed_probe()
    if not info["ok"] or not base_rows:
        return (lexical_rows if lexical_rows is not None else base_rows), False
    vecs = load_vectors([m["id"] for m in base_rows], info["model"])
    if not vecs:
        return (lexical_rows if lexical_rows is not None else base_rows), False
    qv = embed_texts(["search_query: " + q])
    if not qv:
        return (lexical_rows if lexical_rows is not None else base_rows), False

    dim = len(next(iter(vecs.values())))
    ids = list(vecs)
    mean = [0.0] * dim
    for mid in ids:                       # 语料均值向量(用于居中)
        v = vecs[mid]
        for d in range(dim):
            mean[d] += v[d]
    n = len(ids)
    mean = [x / n for x in mean]

    def centered(v):
        return _normalize([v[d] - mean[d] for d in range(dim)])

    qn = centered(qv[0])
    dense = {mid: sum(a * b for a, b in zip(qn, centered(vecs[mid]))) for mid in ids}
    qt = _tokens(q)
    lexical = {m["id"]: _score(qt, m) for m in base_rows}

    def ranks(sc):
        return {k: i for i, k in enumerate(sorted(sc, key=lambda k: -sc[k]))}

    dr, lr = ranks(dense), ranks(lexical)
    BIG = 10 ** 6
    K = int(os.environ.get("VITUAL_RRF_K", "60"))        # RRF 常数
    # 词法权重 >1 的含义:**精确关键词命中永远排在纯语义发现之前**,
    # 语义只负责"词法完全没命中时"的召回(实测该策略兼顾精度与召回)。
    wl = float(os.environ.get("VITUAL_HYBRID_WLEX", "20"))

    def fused(m):
        mid = m["id"]
        return -(wl / (K + lr.get(mid, BIG)) + 1.0 / (K + dr.get(mid, BIG)))

    try:
        min_cos = float(os.environ.get("VITUAL_EMBED_MIN_COS", "0.25"))
    except ValueError:
        min_cos = 0.25
    lex_ids = {m["id"] for m in (lexical_rows if lexical_rows is not None else [])}
    keep = [m for m in base_rows
            if dense.get(m["id"], -1) >= min_cos or m["id"] in lex_ids]
    if not keep:
        return (lexical_rows or []), True

    # 可选 stage-2 重排(cross-encoder):只对召回的 Top-N 精排,其余保持 RRF 顺序附后。
    # 默认关闭(VITUAL_RERANK_MODEL 未设即跳过),零模型依赖下行为与旧版完全一致。
    rerank_model = os.environ.get("VITUAL_RERANK_MODEL", "").strip()
    if rerank_model:
        top_n = min(len(keep), int(os.environ.get("VITUAL_RERANK_TOP", "60")))
        top, rest = keep[:top_n], keep[top_n:]
        docs = [f"{m.get('name', '')} {m.get('description', '')} "
                f"{m.get('tags', '')} {m.get('ai_tags', '')}" for m in top]
        if rerank_model == "lexical":
            order = _lexical_rerank(q, docs, rerank_model)   # 内置离线弱基线,无需 ollama
        else:
            order = _ollama_rerank(q, docs, rerank_model)   # 真目标:ollama cross-encoder
        if order is not None:
            # stage-2 精排生效:直接以重排顺序返回,**不可再用 fused 重排**(否则会覆盖重排结果)
            return [top[i] for i in order] + rest, True

    return sorted(keep, key=fused), True


# 中文 → 语料英文词汇的领域同义词表。
# 必要性:本库元数据是英文(dehardsub/deblur/codeformer/type:subs…),而通常可离线拿到的
# 本地 embedding 模型(nomic-embed-text 等)是**英文单语**,中文问句既匹配不上词法、
# 语义也召不回(实测 P@5=0)。手工维护一张领域词表,秒级生效且完全确定,不依赖大模型。
QUERY_SYNONYMS = {
    # 注意「实体词」与「文件类型词」要分开:
    #   "字幕"        → 指被处理的画面(hardsub/dehardsub),不该把 .srt 顶上来
    #   "字幕文件/文本" → 才指字幕文本本身(srt/ass)
    "字幕": ["subs", "subtitle", "hardsub", "dehardsub"],
    "字幕文件": ["subs", "srt", "ass"],
    "字幕文本": ["subs", "srt", "ass"],
    "文本文件": ["subs", "srt", "ass"],
    "去字幕": ["dehardsub", "hardsub"],
    "软字幕": ["subs", "subtitle"],
    "硬字幕": ["hardsub", "dehardsub"],
    "模糊": ["deblur", "blur"],
    "清晰": ["clean", "deblur"],
    "锐化": ["deblur", "sharpen"],
    "马赛克": ["mosaic"],
    "人脸": ["face", "codeformer", "gfpgan"],
    "修复": ["restore", "codeformer", "fix", "fixed"],
    "增强": ["enhance", "upscale", "esrgan"],
    "对比": ["cmp", "compare", "comparison"],
    "渲染": ["render"],
    "基准": ["benchmark"],
    "探针": ["probe"],
    "片段": ["segments", "segment", "clip"],
    "音频": ["audio", "wav", "m4a"],
    "音轨": ["audio", "wav", "m4a"],
    "视频": ["video", "mp4"],
    "图片": ["image", "png", "jpg"],
    "笔记": ["notes"],
    "翻译": ["translate", "lang"],
    "下载": ["download"],
    "实例": ["instance"],
    "源文件": ["src", "source"],
    "调试": ["debug", "probe"],
    "画质": ["quality"],
    "结果": ["out", "final"],
    # ↓ 2026-09-26 按语料高频 token 挖掘补充(数据驱动,只收语料里真实存在的词)
    "b站": ["bilibili"],
    "裁剪": ["crop"],
    "裁切": ["crop"],
    "帧": ["frames", "frame"],
    "抽帧": ["frames", "frame"],
    "预览": ["preview"],
    "总结": ["summary", "notes"],
    "摘要": ["summary"],
    "快速": ["fast"],
    "填充": ["fill"],
    "显卡": ["gpu"],
    "缺失": ["miss"],
    "丢失": ["miss"],
    "热门": ["hot"],
    "质检": ["qa"],
    "状态": ["status"],
    "元数据": ["meta"],
    "保留": ["keep"],
    "已修复": ["fixed"],
    # ↓ 日常口语补充(数据驱动:对齐语料真实 token out/fixed/final/segment/combine…)
    "修好": ["fixed", "repaired", "out"],
    "成品": ["out", "final", "result"],
    "切": ["cut", "split", "clip", "segment"],
    "时间段": ["segment", "clip", "time"],
    "剪辑": ["edit", "cut", "clip"],
    "切片": ["segment", "clip", "slice"],
    "合成": ["combine", "combined"],
    "合并": ["merge", "combine"],
    "水印": ["watermark", "logo"],
    "台标": ["logo", "watermark"],
    "效果": ["result", "out", "cmp"],
    "对比图": ["cmp", "compare"],
    # 「画面→frame/scene」已删(2026-09-28):泛场景词展开让 name 含 frame 的条目
    # (name 权重 3.0)压过 description 含 lama 的真答案(1.5),Lama 查询 P@5 卡 0.20 根因。
    "背景": ["background", "bg"],
    # ↓ 2026-09-28 由窄查询评估暴露的领域缺口(数据驱动:语料里真实存在的 token)
    "字形": ["glyph"],
    "繁体": ["hant", "zh-hant"],
    "去马赛克": ["demosaic"],
    # 注1:「补全→fill/inpaint」试加过,R@20 0.80→0.20(fill 命名条目稀释),已撤。
    #      Lama 类查询靠查询词自带 lama 字面 token 即可命中。
    # 注2:「画面→frame/scene」已删——泛场景词展开让 name 含 frame 的条目(纯字段名
    #      匹配)压过 description 含 Lama 的真答案(Lama 查询 P@5 0.20 的根因)。
}

# 「泛化词」:命中面太宽,只在查询里没有更具体概念时才展开。
# 实测边界:只收**媒体类型词**(video/mp4/png/jpg…会匹配几百个文件名,
# 纯稀释);而 对比→cmp/compare、结果→out/final 是**答案型词**(compare10s/final_*
# 正是用户要的),降权它们反而把最佳答案挤出前排(渲染结果对比实测回归),保持强展开。
# 2026-09-28:音频/音轨 也移出 weak——weak 是全有全无抑制,查询里只要有 b站/字幕等
# 强概念,音频→audio/m4a 就永不展开;而 m4a 全库仅 2 条,是高精度答案词(b站音频实测)。
_WEAK_SYNONYMS = {"视频", "图片", "状态"}


def expand_query(q):
    """中文问句 → 追加语料里的英文对应词汇(纯英文查询原样返回)。
    只做「加词」不做「改词」,原有命中只会更靠前,不会消失。

    泛化词(_WEAK_SYNONYMS,如 对比/视频/图片)只在查询里**没有更具体概念**时才展开:
    「显卡对比」→ 只加 gpu;单独问「视频」→ 仍展开 video/mp4。"""
    if not q:
        return q
    strong, weak = [], []
    for zh, ens in QUERY_SYNONYMS.items():
        if zh in q:
            (weak if zh in _WEAK_SYNONYMS else strong).extend(ens)
    extra = strong or weak
    if not extra:
        return q
    seen, add = set(), []
    for w in extra:
        if w not in seen:
            seen.add(w)
            add.append(w)
    return q + " " + " ".join(add)


def distinct_tags():
    """返回 [(tag, count), ...],按出现次数倒序。用于面板的标签筛选器。"""
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT tags FROM materials").fetchall()
    con.close()
    cnt = {}
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                cnt[t] = cnt.get(t, 0) + 1
    return sorted(cnt.items(), key=lambda x: -x[1])


def duplicates():
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT sha256, COUNT(*) c, GROUP_CONCAT(id) ids FROM materials GROUP BY sha256 HAVING c>1"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def update_tags(mid, tags, purge_ai_tags=True):
    """更新素材的权威标签列(tags)。

    默认 purge_ai_tags=True:同步清理 ai_tags 中不在新 tags 里的 token。ai_tags 是
    LLM 自动打标产物,常含幻觉(实测 qwen2.5:7b 给烹饪视频打 subs/codeformer),
    使「删除幻觉标签」目标真正落地(agent 与 MCP 的 update_tags 共用本逻辑)。
    返回 {"ok","id","tags","ai_tags_kept","ai_tags_removed"} 供调用方回显。
    """
    old = (get_material(mid) or {}).get("tags", "")
    con = _con()
    con.execute("UPDATE materials SET tags=? WHERE id=?", (tags, mid))
    con.commit()
    con.close()
    log_history("app", "update_tags", mid, f"tags: {old!r} -> {tags!r}")
    kept, removed = [], []
    if purge_ai_tags:
        m = get_material(mid)
        if m:
            ai = [t.strip() for t in (m.get("ai_tags") or "").split(",") if t.strip()]
            want = set(t.strip() for t in tags.split(",") if t.strip())
            for t in ai:
                (kept if t in want else removed).append(t)
            if removed:
                _update_material(mid, ai_tags=",".join(kept))
    return {"ok": True, "id": mid, "tags": tags,
            "ai_tags_kept": kept, "ai_tags_removed": removed}


def update_description(mid, desc):
    old = (get_material(mid) or {}).get("description", "")
    con = _con()
    con.execute("UPDATE materials SET description=? WHERE id=?", (desc, mid))
    con.commit()
    con.close()
    log_history("app", "update_description", mid,
                f"desc: {old!r} -> {desc!r}")


def remove_material(mid):
    m = get_material(mid)
    if m:
        # 外部引用(跨项目联动)只删索引,不动原文件
        if m.get("location") != "external":
            p = m["rel_path"]
            if not os.path.isabs(p):
                p = os.path.join(HUB, p)
            if os.path.exists(p):
                os.makedirs(TRASH, exist_ok=True)
                shutil.move(p, os.path.join(TRASH, os.path.basename(p)))
    con = _con()
    con.execute("DELETE FROM materials WHERE id=?", (mid,))
    con.commit()
    con.close()
    log_history("app", "remove", mid,
                f"removed: {m.get('name', '')} (location={m.get('location', '')})")


# ---------- 采集 / 整理 ----------
def _unique_dest(dest_dir, name):
    dest = os.path.join(dest_dir, name)
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base}-{i}{ext}")
        i += 1
    return dest


def ingest_file(src, move=True, source=""):
    """整理单个文件:去重(按 sha256)、分类、命名规范、入索引。
    返回 {status:'added'|'duplicate', id, path?}。"""
    if not os.path.exists(src):
        return None
    sha = compute_sha256(src)
    con = _con()
    ex = con.execute("SELECT id FROM materials WHERE sha256=?", (sha,)).fetchone()
    con.close()
    if ex:
        if move:
            os.makedirs(TRASH, exist_ok=True)
            shutil.move(src, _unique_dest(TRASH, os.path.basename(src)))
        return {"status": "duplicate", "id": ex[0]}
    kind = classify(src)
    name = sanitize_name(os.path.basename(src))
    dest = _unique_dest(os.path.join(MATERIALS, kind), name)
    if move:
        shutil.move(src, dest)
    else:
        shutil.copy2(src, dest)
    mid = sha[:12]
    add_material(
        id=mid, kind=kind, ext=os.path.splitext(name)[1].lower(), name=name,
        rel_path=os.path.relpath(dest, HUB), size=os.path.getsize(dest), sha256=sha,
        tags="", description="", source=source, orig_name=os.path.basename(src),
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    return {"status": "added", "id": mid, "path": os.path.relpath(dest, HUB)}


def ingest_dir(dirpath, source=""):
    results = []
    for root, _, files in os.walk(dirpath):
        for f in files:
            p = os.path.join(root, f)
            if p.endswith(".db"):
                continue
            r = ingest_file(p, move=True, source=source)
            if r:
                results.append(r)
    return results


def ingest_external(src, source="", tags="", description="", kind=None):
    """登记外部文件(不移动/复制),按原路径引用。用于跨项目联动(如 subtitle_pipeline)。

    视频等大文件不进 materials/ 仓库,只在索引里记 external_path,预览时按需读取,
    避免重复占盘。按 sha256 去重。"""
    src = os.path.abspath(src)
    if not external_path_allowed(src):
        return {"status": "rejected",
                "reason": "external_path outside allowed roots "
                          "(set VITUAL_HUB_EXT_ROOTS to widen)"}
    if not os.path.exists(src) or os.path.isdir(src):
        return None
    sha = compute_sha256(src)
    con = _con()
    ex = con.execute("SELECT id FROM materials WHERE sha256=?", (sha,)).fetchone()
    con.close()
    if ex:
        return {"status": "duplicate", "id": ex[0]}
    k = kind or classify(src)
    name = sanitize_name(os.path.basename(src))
    mid = sha[:12]
    con = _con()
    con.execute(
        """INSERT OR REPLACE INTO materials
           (id,kind,ext,name,rel_path,size,sha256,tags,description,source,orig_name,created_at,location,external_path)
           VALUES(:id,:kind,:ext,:name,:rel_path,:size,:sha256,:tags,:description,:source,:orig_name,:created_at,'external',:external_path)""",
        dict(id=mid, kind=k, ext=os.path.splitext(name)[1].lower(), name=name,
             rel_path=src, size=os.path.getsize(src), sha256=sha, tags=tags,
             description=description, source=source, orig_name=os.path.basename(src),
             created_at=datetime.datetime.now().isoformat(timespec="seconds"),
             external_path=src),
    )
    con.commit()
    con.close()
    return {"status": "added", "id": mid}


def scan_materials():
    """重新扫描 materials/ 全树,补录索引中缺失的文件(基于 sha256 id)。"""
    results = []
    for kind in list(KINDS) + ["other"]:
        d = os.path.join(MATERIALS, kind)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                sha = compute_sha256(p)
                mid = sha[:12]
                con = _con()
                ex = con.execute("SELECT id FROM materials WHERE id=?", (mid,)).fetchone()
                con.close()
                if not ex:
                    add_material(
                        id=mid, kind=kind, ext=os.path.splitext(f)[1].lower(), name=f,
                        rel_path=os.path.relpath(p, HUB), size=os.path.getsize(p), sha256=sha,
                        tags="", description="", source="scan", orig_name=f,
                        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
                    )
                    results.append(mid)
    return results


# ---------- 引用完整性(external 引用巡检) ----------
def broken_externals():
    """列出「失效的外部引用」:source/external 记录指向的原文件已不存在。

    上游项目清理产物、移动目录后常见。索引可随时重建,但先发现才能处理。"""
    out = []
    for m in all_materials():
        if m.get("location") != "external":
            continue
        p = m.get("external_path") or ""
        if not p or not os.path.exists(p):
            out.append(m)
    return out


def prune_broken_externals():
    """删除失效外部引用的索引记录(只删索引,绝不触碰磁盘文件)。返回被清理的记录。"""
    broken = broken_externals()
    if broken:
        con = _con()
        con.executemany("DELETE FROM materials WHERE id=?", [(m["id"],) for m in broken])
        con.commit()
        con.close()
    return broken


def external_stats():
    """外部引用统计:内部素材数 / 外部引用数 / 其中失效数。"""
    ms = all_materials()
    ext = [m for m in ms if m.get("location") == "external"]
    bad = [m for m in ext
           if not (m.get("external_path") and os.path.exists(m["external_path"]))]
    return {"internal": len(ms) - len(ext), "external": len(ext), "broken": len(bad)}


def health():
    """健康检查快照:总量、种类分布、重复、引用完整性、封面能力、磁盘占用。"""
    ms = all_materials()
    kinds = {}
    size = 0
    for m in ms:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
        size += m.get("size") or 0
    st = thumbs_status()
    ex = external_stats()
    videos = kinds.get("videos", 0)
    return {
        "ok": (ex["broken"] == 0),
        "total": len(ms),
        "kinds": kinds,
        "bytes": size,
        "duplicates": len(duplicates()),
        "external": ex,
        "thumbs": {**st, "videos": videos, "missing": max(0, videos - st["cached"] - st["failed"])},
        "db": os.path.relpath(INDEX_DB, HUB),
    }


# ---------- 视频封面(缩略图) ----------
# ffmpeg 探测结果缓存,避免每次请求都扫盘。
_FFMPEG_CACHE = {"path": None, "scanned": False}
# 抽帧并发上限:面板一次列出上百个视频时会并发请求封面,限流避免拉起一堆 ffmpeg 进程。
_THUMB_SLOTS = threading.BoundedSemaphore(int(os.environ.get("VITUAL_THUMB_CONCURRENCY", "2")))


def _ffmpeg_candidates():
    """按优先级产出候选 ffmpeg:环境变量 → PATH → 同工作区项目 venv 自带 → 常见安装位。

    很多 Python 包(imageio-ffmpeg / static-ffmpeg / moviepy)会自带 ffmpeg 二进制,
    虽不在 PATH 上,但可直接调用 —— 自动发现即免安装获得抽帧能力。"""
    import glob as _glob
    env = os.environ.get("VITUAL_FFMPEG", "").strip()
    if env:
        yield env
    got = shutil.which("ffmpeg")
    if got:
        yield got
    root = os.path.dirname(HUB)  # 工作区根目录
    patterns = [
        os.path.join(root, "*", ".venv", "Lib", "site-packages", "static_ffmpeg", "bin", "*", "ffmpeg.exe"),
        os.path.join(root, "*", ".venv", "Lib", "site-packages", "imageio_ffmpeg", "binaries", "ffmpeg*.exe"),
        os.path.join(root, "*", "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(root, "*", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for pat in patterns:
        for hit in sorted(_glob.glob(pat)):
            yield hit


def ffmpeg_path(refresh=False):
    """返回可用的 ffmpeg 可执行文件路径;确实没有则 None。结果缓存。"""
    if refresh or not _FFMPEG_CACHE["scanned"]:
        _FFMPEG_CACHE["scanned"] = True
        _FFMPEG_CACHE["path"] = None
        for c in _ffmpeg_candidates():
            if not c:
                continue
            if os.path.isfile(c):
                _FFMPEG_CACHE["path"] = c
                break
            w = shutil.which(c)
            if w:
                _FFMPEG_CACHE["path"] = w
                break
    return _FFMPEG_CACHE["path"]


def thumb_path(mid):
    return os.path.join(THUMBS, f"{mid}.jpg")


def _abs_source(m):
    """素材的真实磁盘路径(区分内部 / 外部引用)。"""
    if m.get("location") == "external":
        return m.get("external_path") or ""
    p = m["rel_path"]
    return p if os.path.isabs(p) else os.path.join(HUB, p)


def make_thumb(mid, retry_failed=False):
    """为视频抽一帧存成 jpg 封面,缓存到 index/thumbs/<id>.jpg。
    返回封面路径;无 ffmpeg / 非视频 / 抽帧失败均返回 None。

    抽帧失败的会留下 `<id>.jpg.fail` 标记:源文件损坏/未写完时 ffmpeg 很费时,
    标记后不再反复重试(想重试:purge_thumbs() 清标记,或传 retry_failed=True)。"""
    m = get_material(mid)
    if not m or m["kind"] != "videos":
        return None
    dst = thumb_path(mid)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    if not retry_failed and os.path.exists(dst + ".fail"):
        return None
    exe = ffmpeg_path()
    if not exe:
        return None
    src = _abs_source(m)
    if not src or not os.path.exists(src):
        return None
    os.makedirs(THUMBS, exist_ok=True)
    # 默认取第 1 秒(避开片头黑帧);环境变量可调,短视频失败时回退到 0 秒。
    seek = os.environ.get("VITUAL_THUMB_SEEK", "1")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW,避免弹黑框
    last_err = b""
    with _THUMB_SLOTS:                            # 限流:最多同时抽 2 帧
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst                            # 等待期间别人已生成
        for ss in (seek, "0"):
            cmd = [exe, "-v", "error", "-y", "-ss", str(ss), "-i", src,
                   "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", dst]
            try:
                p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   timeout=60, creationflags=flags)
                last_err = (p.stderr or b"")[:400]
            except Exception as e:
                last_err = str(e).encode()
                continue
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                return dst
    # 失败:记录标记(含原因),避免后续每次刷面板都再跑一遍 ffmpeg
    try:
        with open(dst + ".fail", "w", encoding="utf-8") as f:
            f.write(last_err.decode("utf-8", "ignore"))
    except OSError:
        pass
    return None


def thumb_failure_reason(mid):
    """读取失败标记里的 ffmpeg 报错摘要(无标记返回 '')。"""
    p = thumb_path(mid) + ".fail"
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            txt = f.read().strip()
    except OSError:
        return "unreadable"
    lines = [l for l in txt.splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


# ---------- 视频画面 OCR(对齐 §17.2「OCR 入库即可搜」,见最佳实践 §17) ----------
def ocr_sidecar_path(mid):
    """素材的 OCR 文本 sidecar 路径(index/ocr/<id>.txt;派生数据,可随时重建)。"""
    mid = str(mid or "").strip()
    if not mid or any(c in mid for c in "\\/.:"):
        return None                              # 防路径拼接注入
    return os.path.join(OCR_DIR, mid + ".txt")


def _ocr_python():
    """承载 rapidocr 的 python 解释器:优先 VITUAL_OCR_PYTHON,自动发现 SP venv。
    找不到返回 None(OCR 能力优雅缺位,其余功能不受影响)。"""
    env = os.environ.get("VITUAL_OCR_PYTHON", "").strip()
    if env and os.path.isfile(env):
        return env
    cand = os.path.join(os.path.dirname(HUB), "subtitle_pipeline", ".venv",
                        "Scripts", "python.exe")
    return cand if os.path.isfile(cand) else None


def _ffprobe_path():
    exe = ffmpeg_path()
    if not exe:
        return None
    cand = os.path.join(os.path.dirname(exe), "ffprobe.exe")
    if os.name != "nt":
        cand = cand.replace(".exe", "")
    return cand if os.path.isfile(cand) else None


def _probe_duration(path):
    """视频时长(秒);探测失败返回 0(退化为只抽 1 帧)。"""
    exe = _ffprobe_path()
    if not exe:
        return 0.0
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, creationflags=flags)
        return float((p.stdout or b"0").decode("utf-8", "ignore").strip() or 0)
    except Exception:
        return 0.0


def _parse_ocr_runner_stdout(raw):
    """解析 ocr_runner.py 的 stdout JSON(容忍前置日志:取最后一个 '{' 起的 JSON)。"""
    s = (raw or "").decode("utf-8", "ignore")
    i = s.find("[")
    j = s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(s[i:j + 1])
        return data if isinstance(data, list) else None
    except Exception:
        return None


def ocr_material(mid, frames=5, force=False):
    """对视频/图片素材做画面 OCR(离线,rapidocr 由 SP venv 提供):
    ffmpeg 采样帧 → ocr_runner 子进程识别 → 文本落 sidecar `index/ocr/<id>.txt`
    并追加到 description(` [OCR] ...`,词法/语义检索即刻可命中)。
    幂等:已有 sidecar 且未 force 时直接返回 cached。变更记入 history 审计。"""
    m = get_material(mid)
    if not m:
        return {"id": mid, "status": "error", "reason": "not_found"}
    if m.get("kind") not in ("videos", "images"):
        return {"id": mid, "status": "skipped", "reason": f"kind={m.get('kind')}"}
    sc = ocr_sidecar_path(mid)
    if not sc:
        return {"id": mid, "status": "error", "reason": "bad_id"}
    if os.path.isfile(sc) and not force:
        return {"id": mid, "status": "cached", "chars": os.path.getsize(sc)}
    src = _abs_source(m)
    if not src or not os.path.exists(src):
        return {"id": mid, "status": "error", "reason": "source_missing"}
    exe = ffmpeg_path()
    ocr_py = _ocr_python()
    runner = os.path.join(HUB, "ocr_runner.py")
    if not exe:
        return {"id": mid, "status": "skipped", "reason": "no_ffmpeg"}
    if not ocr_py:
        return {"id": mid, "status": "skipped", "reason": "no_ocr_python"}
    if m["kind"] == "images":
        # 统一经 ffmpeg 标准化成 jpg 再识别:GIF(动图)/HEIC 等格式 cv2/RapidOCR 读不了,
        # 而 ffmpeg 对所有图片格式通吃(GIF 取首帧动图起点);jpg/png 也顺手统一压缩带宽。
        flags = 0x08000000 if os.name == "nt" else 0
        std = os.path.join(OCR_DIR, "_f0.jpg")
        cmd = [exe, "-v", "error", "-y", "-i", src, "-frames:v", "1",
               "-vf", "scale=1280:-2", "-q:v", "4", std]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60,
                           creationflags=flags)
        except Exception:
            std = None
        if not std or not os.path.exists(std) or os.path.getsize(std) == 0:
            return {"id": mid, "status": "error", "reason": "frame_extract_failed"}
        imgs, stamps = [(std, 0.0)], [None]
    else:
        dur = _probe_duration(src)
        frames = max(1, min(int(frames or 5), 10))
        if dur > 0.5:
            stamps = [round(dur * f, 2) for f in (0.1, 0.3, 0.5, 0.7, 0.9)][:frames]
        else:
            stamps = [0.0]
        flags = 0x08000000 if os.name == "nt" else 0
        imgs = []
        for i, t in enumerate(stamps):
            out = os.path.join(OCR_DIR, f"_f{i}.jpg")
            cmd = [exe, "-v", "error", "-y", "-ss", str(t), "-i", src,
                   "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "4", out]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60,
                               creationflags=flags)
            except Exception:
                continue
            if os.path.exists(out) and os.path.getsize(out) > 0:
                imgs.append((out, t))
        if not imgs:
            return {"id": mid, "status": "error", "reason": "frame_extract_failed"}
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")   # 防 GBK 管道乱码(双保险)
        p = subprocess.run([ocr_py, runner] + [f for f, _ in imgs],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=600, creationflags=0x08000000 if os.name == "nt" else 0,
                           env=env)
        data = _parse_ocr_runner_stdout(p.stdout)
    except Exception:
        data = None
    if data is None:
        return {"id": mid, "status": "error", "reason": "ocr_runner_failed"}
    by_file = {d.get("file", ""): d for d in data}
    lines = []
    total = 0
    for f, t in imgs:
        d = by_file.get(f) or {}
        txt = (d.get("text") or "").strip()
        if txt:
            tag = f"[{t}s] " if t is not None else ""
            lines.append(f"{tag}{txt}")
            total += len(txt)
        try:
            if f != src:
                os.remove(f)                     # 清理临时抽帧
        except OSError:
            pass
    os.makedirs(OCR_DIR, exist_ok=True)
    with open(sc, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # description 追加 OCR 摘要(幂等:先剥旧 [OCR] 段),使主检索/语义建库即刻可命中
    cur_desc = (m.get("description") or "").strip()
    base = re.sub(r"\s*\[OCR\][\s\S]*$", "", cur_desc).strip()
    new_desc = (base + ("\n" if base else "") + "[OCR] " + "\n".join(lines)[:400]).strip()
    if new_desc != cur_desc:
        _update_material(mid, description=new_desc)
    log_history("app", "ocr", mid, f"frames={len(imgs)} chars={total}")
    return {"id": mid, "status": "ok", "frames": len(imgs), "chars": total,
            "sample": "\n".join(lines)[:200]}


def ocr_all(limit=0, force=False):
    """批量 OCR:对全部视频/图片素材补齐 sidecar(默认跳过已有)。"""
    out = []
    ms = [m for m in all_materials() if m.get("kind") in ("videos", "images")]
    if limit:
        ms = ms[:limit]
    for m in ms:
        sc = ocr_sidecar_path(m["id"])
        if not force and sc and os.path.isfile(sc):
            continue
        out.append(ocr_material(m["id"], force=force))
    return out


# ---------- 镜头索引(ffmpeg 场景检测,片段级输出,供下游剪辑 Agent 按片段调用) ----------
def shot_index_path(mid):
    """素材的镜头索引 sidecar 路径(index/shots/<id>.json;派生数据,可随时重建)。
    mid 含路径分隔符/冒号/点或为空 → None(防路径拼接注入,与 ocr_sidecar_path 同构)。"""
    mid = str(mid or "").strip()
    if not mid or any(c in mid for c in "\\/.:"):
        return None                              # 防路径拼接注入
    return os.path.join(SHOT_DIR, mid + ".json")


# 场景检测阈值阶梯:切点过多(>max_scenes)时逐级提高灵敏度门槛重试,直到切点数可接受。
_SHOT_THRESHOLDS = (0.30, 0.40, 0.50)


def _ffmpeg_scene_cuts(exe, src, threshold):
    """跑一遍 ffmpeg 场景检测,返回切点列表(秒,升序去重)。

    用 select='gt(scene,t)' 过滤镜头切换帧,showinfo 把被选中的帧打印到 stderr,
    从 stderr 解析 `pts_time:([0-9.]+)` 即切点。子进程失败返回 None(调用方报 error)。"""
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW,避免弹黑框
    try:
        p = subprocess.run(
            [exe, "-v", "warning", "-i", src,
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=300, creationflags=flags)
    except Exception:
        return None
    txt = (p.stderr or b"").decode("utf-8", "ignore")
    cuts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", txt)]
    return sorted(set(cuts))


def build_shot_index(mid, force=False, max_scenes=60):
    """为视频建立镜头索引:ffmpeg 场景检测 → 切点 → 片段级 start/end 时间轴,
    落 sidecar `index/shots/<id>.json`。幂等:已有 sidecar 且未 force → cached。
    变更记入 history 审计。返回 dict,状态 ∈ ok|cached|skipped|error。"""
    m = get_material(mid)
    if not m:
        return {"id": mid, "status": "error", "reason": "not_found"}
    if m.get("kind") != "videos":
        return {"id": mid, "status": "skipped", "reason": f"kind={m.get('kind')}"}
    sc = shot_index_path(mid)
    if not sc:
        return {"id": mid, "status": "error", "reason": "bad_id"}
    if os.path.isfile(sc) and not force:
        n = 0
        try:
            with open(sc, "r", encoding="utf-8") as f:
                n = len(json.load(f).get("scenes", []))
        except Exception:
            pass
        return {"id": mid, "status": "cached", "scenes": n}
    src = _abs_source(m)
    if not src or not os.path.exists(src):
        return {"id": mid, "status": "error", "reason": "source_missing"}
    exe = ffmpeg_path()
    if not exe:
        return {"id": mid, "status": "skipped", "reason": "no_ffmpeg"}
    dur = _probe_duration(src)
    # 阈值阶梯:切点过多就提高阈值重跑(重新跑 ffmpeg),直到 ≤max_scenes 或阈值用尽
    cuts, thr = [], None
    for t in _SHOT_THRESHOLDS:
        c = _ffmpeg_scene_cuts(exe, src, t)
        if c is None:
            return {"id": mid, "status": "error", "reason": "ffmpeg_failed"}
        cuts, thr = c, t
        if len(cuts) <= max_scenes:
            break
    # 切点 → 片段:首段 start=0,末段 end=duration;相邻切点间为一段。
    # dur 探测失败(=0)时用最后切点兜底,避免末段被截成空区间。
    last = max(dur, cuts[-1] if cuts else 0.0)
    bounds = [0.0] + [c for c in cuts if 0.0 < c < last] + [last]
    scenes = [{"start": round(a, 3), "end": round(b, 3)}
              for a, b in zip(bounds, bounds[1:]) if b > a]
    if not scenes:                       # 无切点(静态视频)→ 单段全覆盖
        scenes = [{"start": 0.0, "end": round(last, 3)}]
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(sc, "w", encoding="utf-8") as f:
        json.dump({"duration": dur, "threshold": thr, "scenes": scenes},
                  f, ensure_ascii=False)
    log_history("app", "shot_index", mid, f"scenes={len(scenes)}")
    return {"id": mid, "status": "ok", "scenes": len(scenes), "duration": dur}


def get_shots(mid):
    """读取镜头索引 sidecar(返回 dict);不存在/不可读返回 None。只读、不写。"""
    sc = shot_index_path(mid)
    if not sc or not os.path.isfile(sc):
        return None
    try:
        with open(sc, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def shots_all(limit=0, force=False):
    """批量镜头索引:对全部视频素材补齐(默认跳过已有 sidecar),仿 ocr_all。"""
    out = []
    ms = [m for m in all_materials() if m.get("kind") == "videos"]
    if limit:
        ms = ms[:limit]
    for m in ms:
        sc = shot_index_path(m["id"])
        if not force and sc and os.path.isfile(sc):
            continue
        out.append(build_shot_index(m["id"], force=force))
    return out


# ---------- pHash 近重复检测(dHash 差异哈希:纯 Python 可算,无需 DCT) ----------
# 与 duplicates() 的 sha256 精确去重互补:感知哈希能检出转码/重采样/压缩后的同画面素材。
def phash_path(mid):
    """素材的 dHash sidecar 路径(index/phash/<id>.txt;派生数据,可随时重建)。
    mid 含路径分隔符/冒号/点或为空 → None(防路径拼接注入,与 ocr_sidecar_path 同构)。"""
    mid = str(mid or "").strip()
    if not mid or any(c in mid for c in "\\/.:"):
        return None                              # 防路径拼接注入
    return os.path.join(PHASH_DIR, mid + ".txt")


def _bytes_to_dhash(data):
    """把 72 字节(9 列×8 行,每像素 1 字节)灰度原始像素转成 64 位 dHash int。

    每行内相邻像素比较(左>右 → 该位为 1),8 行 × 8 比较 = 64 位,
    第 row*8+col 位对应第 row 行第 col 对。长度不足 72 字节 → None(帧提取失败)。"""
    if len(data) < 72:
        return None
    h = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            if data[base + col] > data[base + col + 1]:
                h |= 1 << (row * 8 + col)
    return h


def _ffmpeg_dhash_bits(exe, src):
    """用 ffmpeg 把首帧缩成 9x8 灰度原始像素,交给 _bytes_to_dhash 算 64 位 dHash。
    子进程失败或像素不足 → None。视频只取首帧(近重复检测足够)。"""
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW,避免弹黑框
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-i", src, "-frames:v", "1",
             "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=60, creationflags=flags)
    except Exception:
        return None
    return _bytes_to_dhash(p.stdout or b"")


def hamming(a, b):
    """两个 int 的汉明距离(不同位的个数)。"""
    return bin(a ^ b).count("1")


def phash_material(mid, force=False):
    """为图片/视频素材计算 dHash 感知哈希,落 sidecar `index/phash/<id>.txt`(16 位 hex)。
    幂等:已有 sidecar 且未 force → cached。变更记入 history 审计。
    返回 dict,状态 ∈ ok|cached|skipped|error(仿 ocr_material 的状态模式)。"""
    m = get_material(mid)
    if not m:
        return {"id": mid, "status": "error", "reason": "not_found"}
    if m.get("kind") not in ("images", "videos"):
        return {"id": mid, "status": "skipped", "reason": f"kind={m.get('kind')}"}
    sc = phash_path(mid)
    if not sc:
        return {"id": mid, "status": "error", "reason": "bad_id"}
    if os.path.isfile(sc) and not force:
        try:
            with open(sc, "r", encoding="utf-8") as f:
                return {"id": mid, "status": "cached", "hash": f.read().strip()}
        except OSError:
            pass                                  # sidecar 损坏就当没有,重算
    src = _abs_source(m)
    if not src or not os.path.exists(src):
        return {"id": mid, "status": "error", "reason": "source_missing"}
    exe = ffmpeg_path()
    if not exe:
        return {"id": mid, "status": "skipped", "reason": "no_ffmpeg"}
    bits = _ffmpeg_dhash_bits(exe, src)
    if bits is None:
        return {"id": mid, "status": "error", "reason": "frame_extract_failed"}
    hx = f"{bits:016x}"
    os.makedirs(PHASH_DIR, exist_ok=True)
    with open(sc, "w", encoding="utf-8") as f:
        f.write(hx)
    log_history("app", "phash", mid, f"hash={hx}")
    return {"id": mid, "status": "ok", "hash": hx}


def phash_all(limit=0, force=False):
    """批量 pHash:对全部图片/视频素材补齐(默认跳过已有 sidecar),仿 ocr_all。"""
    out = []
    ms = [m for m in all_materials() if m.get("kind") in ("images", "videos")]
    if limit:
        ms = ms[:limit]
    for m in ms:
        sc = phash_path(m["id"])
        if not force and sc and os.path.isfile(sc):
            continue
        out.append(phash_material(m["id"], force=force))
    return out


def similar_assets(mid, max_dist=10):
    """画面级近重复检测:拿自身 dHash 与库内其他素材的 sidecar 逐一算汉明距离,
    距离 ≤ max_dist 的按距离升序返回 [{"id","name","dist"}]。纯只读;
    自身无 sidecar → {"status":"no_hash"};total = 参与比较的素材数。"""
    sc = phash_path(mid)
    if not sc or not os.path.isfile(sc):
        return {"id": mid, "status": "no_hash", "similar": [], "total": 0}
    try:
        with open(sc, "r", encoding="utf-8") as f:
            mine = int(f.read().strip(), 16)
    except (OSError, ValueError):
        return {"id": mid, "status": "no_hash", "similar": [], "total": 0}
    hits, total = [], 0
    if os.path.isdir(PHASH_DIR):
        for fn in os.listdir(PHASH_DIR):
            if not fn.endswith(".txt"):
                continue
            oid = fn[:-len(".txt")]
            if oid == mid:
                continue                        # 跳过自身
            try:
                with open(os.path.join(PHASH_DIR, fn), "r", encoding="utf-8") as f:
                    other = int(f.read().strip(), 16)
            except (OSError, ValueError):
                continue                        # 坏 sidecar 不参与比较
            total += 1
            d = hamming(mine, other)
            if d <= max_dist:
                hits.append({"id": oid,
                             "name": (get_material(oid) or {}).get("name", ""),
                             "dist": d})
    hits.sort(key=lambda x: x["dist"])
    return {"id": mid, "status": "ok", "similar": hits, "total": total}


# ---------- 事件驱动自动处理链(封面→OCR→镜头索引→pHash→自动打标→语义索引) ----------
def pending_processing(mid):
    """返回该素材还缺哪些自动处理步骤(True=缺)。

    判断依据:thumb 看 thumb_path(mid) 的 jpg 是否存在且非空;
    ocr/shots/phash 看各自 sidecar(index/ocr|shots|phash/<id>.*)是否已落盘。
    非 videos/images 素材(docs/subs/audio/other)不需要画面类派生数据 → 全 False。"""
    m = get_material(mid)
    if not m or m.get("kind") not in ("videos", "images"):
        return {"thumb": False, "ocr": False, "shots": False, "phash": False}
    tp = thumb_path(mid)
    op, sp, pp = ocr_sidecar_path(mid), shot_index_path(mid), phash_path(mid)
    return {
        "thumb": not (tp and os.path.exists(tp) and os.path.getsize(tp) > 0),
        "ocr": not (op and os.path.isfile(op)),
        "shots": not (sp and os.path.isfile(sp)),
        "phash": not (pp and os.path.isfile(pp)),
    }


def auto_process_material(mid, ocr=True, shots=True, phash=True, autotag=False):
    """对单条素材按需依序执行自动处理链:make_thumb → ocr_material →
    build_shot_index → phash_material(autotag=True 再追加 auto_tag_material)。

    每步独立 try/except 包裹:单项失败记 error 但不中断后续步骤;
    各步骤自身幂等(已处理过 → cached/skip),重复跑无害。
    返回 {"id", "steps": {步名: 结果 dict}, "ok", "total"},
    并 log_history("app","auto_process",mid,"ok=N/M")。"""
    steps = {}
    total = okn = 0

    def _attempt(name, fn):
        nonlocal total, okn
        total += 1
        try:
            r = fn()
        except Exception as e:               # 单项失败不中断整链
            r = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        steps[name] = r
        if isinstance(r, dict) and r.get("status") in ("ok", "cached"):
            okn += 1

    def _thumb():
        p = make_thumb(mid)
        # make_thumb 返回路径或 None(非视频/无 ffmpeg/失败均有 .fail 标记防重试)
        return {"status": "ok", "path": p} if p else {"status": "skip", "reason": "no_thumb"}

    _attempt("thumb", _thumb)
    if ocr:
        _attempt("ocr", lambda: ocr_material(mid))
    if shots:
        _attempt("shots", lambda: build_shot_index(mid))
    if phash:
        _attempt("phash", lambda: phash_material(mid))
    if autotag:
        m = get_material(mid)
        if m:                                # 无 chat 模型时 auto_tag_material 自动 skipped
            _attempt("autotag", lambda: auto_tag_material(m))
    log_history("app", "auto_process", mid, f"ok={okn}/{total}")
    return {"id": mid, "steps": steps, "ok": okn, "total": total}


def auto_process_all(limit=0, autotag=False):
    """批处理:找出所有还有缺项的 videos/images 素材,逐条跑 auto_process_material,
    最后调一次 build_embeddings()(增量:只补新素材/文本有变化的向量)。
    limit>0 时只处理前 limit 条。返回 {"processed", "results", "embed"}。"""
    ms = [m for m in all_materials()
          if m.get("kind") in ("videos", "images")
          and any(pending_processing(m["id"]).values())]
    if limit:
        ms = ms[:limit]
    results = [auto_process_material(m["id"], autotag=autotag) for m in ms]
    emb = build_embeddings()                 # 增量语义索引;无 ollama 时自动 available=False
    return {"processed": len(results), "results": results, "embed": emb}


def thumbs_status():
    """封面能力状态:ffmpeg 是否可用 + 已缓存数量 + 抽帧失败数。"""
    n = fail = 0
    if os.path.isdir(THUMBS):
        for f in os.listdir(THUMBS):
            if f.endswith(".jpg.fail"):
                fail += 1
            elif f.endswith(".jpg"):
                n += 1
    exe = ffmpeg_path()
    return {"ffmpeg": bool(exe), "exe": os.path.basename(exe) if exe else "",
            "cached": n, "failed": fail, "dir": os.path.relpath(THUMBS, HUB)}


def purge_thumbs():
    """清空封面缓存与失败标记(派生数据,删除无副作用)。返回清理数量。"""
    n = 0
    if os.path.isdir(THUMBS):
        for f in os.listdir(THUMBS):
            p = os.path.join(THUMBS, f)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    n += 1
                except OSError:
                    pass
    return n


def missing_thumbnail_ids(skip_failed=True):
    """列出「还没有封面」的视频素材 id(供批量生成)。
    skip_failed=True 时跳过已知损坏(有 .fail 标记)的,避免每次批量都白跑。"""
    out = []
    for m in all_materials():
        if m["kind"] != "videos":
            continue
        p = thumb_path(m["id"])
        if os.path.exists(p) and os.path.getsize(p) > 0:
            continue
        if skip_failed and os.path.exists(p + ".fail"):
            continue
        out.append(m["id"])
    return out


if __name__ == "__main__":
    init_hub()
    print("hub initialized at", HUB)
