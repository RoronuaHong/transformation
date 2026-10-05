"""Materials Hub — 核心库(零依赖,Python 3.12)。

负责:目录初始化、文件分类、SHA-256 去重、命名规范化、SQLite 索引、检索。
所有路径相对 HUB 根,索引库为 index/hub.db。
"""
import os
import re
import json
import time
import obs                                   # 可观测:结构化日志 + 指标埋点(§8 G 维度)
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

# kinds that get ffmpeg thumbs / visual auto-process
VIDEO_LIKE_KINDS = ("videos", "silent")
VISUAL_KINDS = ("videos", "silent", "images")
# 按扩展名分类。video 容器再按「是否有音轨」拆成 videos / silent（无声）。
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts"}
KINDS = {
    "images": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".svg", ".tif", ".tiff", ".ico", ".heic"},
    "anim": {".gif"},
    "videos": set(VIDEO_EXTS),          # 有音轨的画面
    "silent": set(VIDEO_EXTS),          # 无声音轨的画面（同扩展名，靠探测分流）
    "docs": {".pdf", ".md", ".txt", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
             ".csv", ".json", ".yaml", ".yml", ".html", ".epub", ".rtf"},
    "audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"},  # 纯音轨
    "subs": {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sbv"},
}


def init_hub():
    """创建标准目录结构并初始化数据库。幂等。"""
    for d in [MATERIALS, INGEST, TRASH, INDEX_DIR, OCR_DIR, SHOT_DIR, PHASH_DIR, STATIC]:
        os.makedirs(d, exist_ok=True)
    for k in list(KINDS) + ["other"]:
        os.makedirs(os.path.join(MATERIALS, k), exist_ok=True)
    _init_db()


def probe_has_audio(path, timeout=20):
    """True when the file has at least one audio stream (ffprobe preferred).

    Fail-open → True（探测失败仍归 videos，避免误丢进无声）。
    """
    if not path or not os.path.isfile(path):
        return True
    exe = _ffprobe_path()
    flags = 0x08000000 if os.name == "nt" else 0
    if exe:
        try:
            p = subprocess.run(
                [
                    exe, "-v", "error", "-select_streams", "a",
                    "-show_entries", "stream=codec_type",
                    "-of", "csv=p=0", path,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout, creationflags=flags,
            )
            out = (p.stdout or b"").decode("utf-8", "ignore").strip()
            if out:
                return True
            # empty stdout with exit 0 → no audio streams
            if p.returncode == 0:
                return False
        except Exception:
            pass
    # Fallback: ffmpeg -i stderr scan
    ff = ffmpeg_path()
    if not ff:
        return True
    try:
        p = subprocess.run(
            [ff, "-i", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=flags,
        )
        err = (p.stderr or b"").decode("utf-8", "ignore").lower()
        if re.search(r"audio:\s*\w+", err) or re.search(
            r"stream\s+#\d+:\d+.*?\baudio\b", err
        ):
            return True
        if re.search(r"video:\s*\w+", err) or re.search(
            r"stream\s+#\d+:\d+.*?\bvideo\b", err
        ):
            return False
    except Exception:
        return True
    return True


def is_physical_clip_path(path):
    """SP 交付切片: media/clips/range_XX.mp4（逻辑镜头仍只在 shots sidecar）。"""
    low = (path or "").replace("\\", "/").lower()
    base = os.path.basename(low)
    if "/media/clips/" in low:
        return True
    if re.match(r"range_\d+\.(mp4|mkv|mov|webm)$", base):
        return True
    return False


def load_clip_timecodes(path):
    """从同目录 clips_meta.json 取该切片在母版上的 (t_start, t_end)；没有则 (None, None)。"""
    if not path:
        return None, None
    meta_path = os.path.join(os.path.dirname(path), "clips_meta.json")
    if not os.path.isfile(meta_path):
        return None, None
    try:
        with open(meta_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None, None
    spans = data.get("spans") if isinstance(data, dict) else None
    if not isinstance(spans, list):
        return None, None
    base = os.path.basename(path)
    for row in spans:
        if not isinstance(row, dict):
            continue
        if (row.get("file") or "") != base:
            continue
        try:
            return float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            return None, None
    return None, None


def relation_tags(parent_id=None, t_start=None, t_end=None):
    """母版–子件关系标签: parent: + 可选时间码(物理 clip 用)。

    不再双写 from:(与 parent: 同义,面板易混淆);读路径仍识别旧 from:。"""
    tags = []
    pid = (parent_id or "").strip()
    if pid:
        tags.append("parent:" + pid)
    if t_start is not None and t_end is not None:
        try:
            tags.append("t_start:%.3f" % float(t_start))
            tags.append("t_end:%.3f" % float(t_end))
        except (TypeError, ValueError):
            pass
    return tags


def inherit_parent_context_tags(parent_id):
    """子件继承母版上下文: sp / job: / 平台名(便于 job_checkup 同筛)。"""
    m = get_material(parent_id) if parent_id else None
    if not m:
        return []
    out = []
    for t in _material_tag_set(m):
        if t == "sp" or t.startswith("job:") or t in (
            "bilibili", "youtube", "upload", "sp",
        ):
            out.append(t)
    return out


def _material_tag_set(m_or_tags):
    if isinstance(m_or_tags, dict):
        raw = m_or_tags.get("tags") or ""
    elif isinstance(m_or_tags, (set, list, tuple)):
        return {str(t).strip() for t in m_or_tags if str(t).strip()}
    else:
        raw = m_or_tags or ""
    return {t.strip() for t in raw.split(",") if t.strip()}


def is_role_clip(m_or_tags):
    return "role:clip" in _material_tag_set(m_or_tags)


def is_role_master(m_or_tags):
    tags = _material_tag_set(m_or_tags)
    return "role:master" in tags and "role:clip" not in tags


def coerce_role_tags(tags_csv, *, kind=None, path=None):
    """互斥整理 role:：clip 与 master 不同时存在；非 clip 的 videos 补 role:master。"""
    parts = [t.strip() for t in (tags_csv or "").split(",") if t.strip()]
    tags = set(parts)
    path_is_clip = bool(path) and is_physical_clip_path(path)
    if path_is_clip or "role:clip" in tags or "type:clip" in tags:
        tags.add("role:clip")
        tags.add("type:clip")
        tags.discard("role:master")
        tags.discard("role:picture")
    elif (kind or "") == "videos" or "role:picture" in tags:
        if "role:clip" not in tags:
            tags.add("role:master")
            if "has_audio:0" not in tags:
                tags.add("role:picture")
    out = []
    seen = set()
    for t in parts:
        if t not in tags or t in seen:
            continue
        out.append(t)
        seen.add(t)
    for t in ("role:master", "role:picture", "role:clip", "type:clip"):
        if t in tags and t not in seen:
            out.append(t)
            seen.add(t)
    for t in sorted(tags):
        if t not in seen:
            out.append(t)
            seen.add(t)
    return ",".join(out)


def media_facet_tags(path, kind=None):
    """DAM 面标签: master / clip / silent-picture / audio-stem。

    四层模型(方案 A): 母版文件 + shots 逻辑切片 + silent/audio 组件 + 可选物理 clip。
    物理切片路径 → role:clip(不当 master); 有声源片 → role:master+role:picture。
    """
    kind = kind or classify(path)
    name = os.path.basename(path or "").lower()
    tags = []
    if kind == "silent":
        tags.extend(["role:silent-picture", "has_audio:0"])
    elif kind == "videos":
        if is_physical_clip_path(path):
            tags.extend(["role:clip", "type:clip", "has_audio:1"])
            t0, t1 = load_clip_timecodes(path)
            tags.extend(relation_tags(t_start=t0, t_end=t1))
        else:
            tags.extend(["role:master", "role:picture", "has_audio:1"])
    elif kind == "audio":
        tags.append("role:audio-stem")
        tags.append("has_audio:1")
        if "16k" in name or name.startswith("full_16k"):
            tags.append("asr")
    return tags


def _merge_tag_csv(old, extra):
    parts = [t.strip() for t in (old or "").split(",") if t.strip()]
    for t in extra or []:
        t = (t or "").strip()
        if t and t not in parts:
            parts.append(t)
    return ",".join(parts)


def _masters_by_job(all_ms=None):
    """job: → 最佳母版 id(优先 role:master)。"""
    all_ms = all_ms if all_ms is not None else all_materials()
    best = {}
    for x in all_ms:
        if x.get("kind") != "videos" or is_role_clip(x):
            continue
        xt = _material_tag_set(x)
        for t in xt:
            if not t.startswith("job:"):
                continue
            cur = best.get(t)
            if cur is None or "role:master" in xt:
                best[t] = x["id"]
    return best


def _video_stem_index(all_ms=None):
    """文件名 stem → 母版 id(非 clip 的 videos)。"""
    all_ms = all_ms if all_ms is not None else all_materials()
    by_stem = {}
    for x in all_ms:
        if x.get("kind") != "videos" or is_role_clip(x):
            continue
        stem = os.path.splitext(x.get("name") or "")[0].lower()
        if stem:
            by_stem[stem] = x["id"]
    return by_stem


def _component_stem(name):
    stem = os.path.splitext(name or "")[0].lower()
    for suffix in ("_silent", "_track", "_audio", "_stem"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def link_clip_parents(*, mids=None, limit=0):
    """给 role:clip 且缺 parent: 的条目补 parent:<同 job 母版 id>。

    传入 mids 时:处理这些 id 及其同 job 下缺 parent 的 clip。
    返回 {scanned, linked, skipped}。
    """
    all_ms = all_materials()
    masters_by_job = _masters_by_job(all_ms)
    mid_set = set(mids or [])
    jobs = set()
    if mid_set:
        for m in all_ms:
            if m["id"] not in mid_set:
                continue
            for t in _material_tag_set(m):
                if t.startswith("job:"):
                    jobs.add(t)
    rows = []
    for m in all_ms:
        if not is_role_clip(m):
            continue
        if mid_set:
            tags = _material_tag_set(m)
            same_job = bool(jobs and any(j in tags for j in jobs))
            if m["id"] not in mid_set and not same_job:
                continue
        rows.append(m)
    scanned = linked = skipped = 0
    for m in rows:
        if limit and scanned >= limit:
            break
        scanned += 1
        tags = _material_tag_set(m)
        if any(t.startswith("parent:") for t in tags):
            skipped += 1
            continue
        job = next((t for t in tags if t.startswith("job:")), "")
        mid = masters_by_job.get(job) if job else None
        if not mid:
            skipped += 1
            continue
        _update_material(
            m["id"],
            tags=_merge_tag_csv(
                m.get("tags"),
                relation_tags(mid) + inherit_parent_context_tags(mid),
            ),
        )
        linked += 1
    return {"scanned": scanned, "linked": linked, "skipped": skipped}


def link_component_parents(*, limit=0):
    """给 silent/audio 缺 parent: 的组件回挂母版(可读旧 from: / 文件名 stem / 同 job)。"""
    all_ms = all_materials()
    by_stem = _video_stem_index(all_ms)
    masters_by_job = _masters_by_job(all_ms)
    scanned = linked = skipped = 0
    for m in all_ms:
        if m.get("kind") not in ("silent", "audio"):
            continue
        if limit and scanned >= limit:
            break
        scanned += 1
        tags = _material_tag_set(m)
        if any(t.startswith("parent:") for t in tags):
            pid = next(t[7:] for t in tags if t.startswith("parent:"))
            need = list(inherit_parent_context_tags(pid))
            if "demux" not in tags and (m.get("source") or "").startswith("demux"):
                need.append("demux")
            # 只补缺失项
            need = [t for t in need if t not in tags]
            if need:
                _update_material(m["id"], tags=_merge_tag_csv(m.get("tags"), need))
                linked += 1
            else:
                skipped += 1
            continue
        pid = ""
        fr = next((t[5:] for t in tags if t.startswith("from:")), "")
        if fr and get_material(fr):
            pid = fr
        if not pid:
            pid = by_stem.get(_component_stem(m.get("name") or "")) or ""
        if not pid:
            job = next((t for t in tags if t.startswith("job:")), "")
            if job:
                pid = masters_by_job.get(job) or ""
        if not pid:
            skipped += 1
            continue
        extra = list(relation_tags(pid)) + inherit_parent_context_tags(pid)
        name_l = (m.get("name") or "").lower()
        if "demux" not in tags and (
            name_l.endswith("_silent.mp4")
            or name_l.endswith("_track.wav")
            or (m.get("source") or "").startswith("demux")
        ):
            extra.insert(0, "demux")
        # 已有 parent 但缺 job: 时也补上下文
        _update_material(m["id"], tags=_merge_tag_csv(m.get("tags"), extra))
        linked += 1
    return {"scanned": scanned, "linked": linked, "skipped": skipped}


def link_relation_parents(*, mids=None, limit=0):
    """方案 A 关系回填:物理 clip + silent/audio 组件 → parent:。"""
    a = link_clip_parents(mids=mids, limit=limit)
    b = link_component_parents(limit=limit)
    return {
        "clips": a,
        "components": b,
        "linked": a.get("linked", 0) + b.get("linked", 0),
    }


def classify(path):
    """扩展名初分；视频容器再按音轨拆 videos / silent。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "videos" if probe_has_audio(path) else "silent"
    for k, exts in KINDS.items():
        if k in ("videos", "silent"):
            continue
        if ext in exts:
            return k
    return "other"


def reclassify_video_audio_kinds(limit=0):
    """把已入库的视频容器按音轨重分到 videos / silent。返回统计。"""
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, kind, location, external_path, rel_path FROM materials "
        "WHERE kind IN ('videos','silent') OR ext IN ('.mp4','.mkv','.mov','.avi','.webm','.m4v','.flv','.ts')"
    ).fetchall()
    con.close()
    changed = {"to_videos": 0, "to_silent": 0, "skipped": 0, "missing": 0}
    n = 0
    for r in rows:
        if limit and n >= limit:
            break
        n += 1
        p = r["external_path"] if (r["location"] or "") == "external" else None
        if not p:
            # internal: MATERIALS/kind/rel or rel_path
            rp = r["rel_path"] or ""
            p = rp if os.path.isabs(rp) else os.path.join(HUB, rp)
        if not p or not os.path.isfile(p):
            changed["missing"] += 1
            continue
        want = classify(p)
        if want == r["kind"]:
            extra = media_facet_tags(p, want)
            cur = get_material(r["id"])
            merged = coerce_role_tags(
                _merge_tag_csv(cur.get("tags") if cur else "", extra),
                kind=want, path=p,
            )
            if cur and merged != (cur.get("tags") or ""):
                _update_material(r["id"], tags=merged)
            changed["skipped"] += 1
            continue
        extra = media_facet_tags(p, want)
        cur = get_material(r["id"])
        _update_material(
            r["id"],
            kind=want,
            tags=coerce_role_tags(
                _merge_tag_csv(cur.get("tags") if cur else "", extra),
                kind=want, path=p,
            ),
        )
        if want == "silent":
            changed["to_silent"] += 1
        else:
            changed["to_videos"] += 1
    return changed


def apply_media_facet_tags(limit=0):
    """给已入库 videos/silent/audio 补 DAM 面标签，不改变 kind。"""
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, kind, location, external_path, rel_path, tags FROM materials "
        "WHERE kind IN ('videos','silent','audio')"
    ).fetchall()
    con.close()
    n = patched = 0
    for r in rows:
        if limit and n >= limit:
            break
        n += 1
        p = r["external_path"] if (r["location"] or "") == "external" else None
        if not p:
            rp = r["rel_path"] or ""
            p = rp if os.path.isabs(rp) else os.path.join(HUB, rp)
        # 路径缺失时仍可按 kind 补角色面(不依赖磁盘)
        kind = r["kind"] or (classify(p) if p and os.path.isfile(p) else None)
        if not kind:
            continue
        if p and os.path.isfile(p):
            extra = media_facet_tags(p, kind)
        elif kind == "videos":
            extra = ["role:master", "role:picture", "has_audio:1"]
        elif kind == "silent":
            extra = ["role:silent-picture", "has_audio:0"]
        elif kind == "audio":
            extra = ["role:audio-stem", "has_audio:1"]
        else:
            continue
        merged = coerce_role_tags(
            _merge_tag_csv(r["tags"], extra), kind=kind, path=p if p and os.path.isfile(p) else None
        )
        if merged != (r["tags"] or ""):
            _update_material(r["id"], tags=merged)
            patched += 1
    return {"scanned": n, "patched": patched}


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
    # 图像 CLIP 向量(与文本 embeddings 分表;model 形如 ViT-B-32/openai)
    con.execute(
        """CREATE TABLE IF NOT EXISTS image_embeddings(
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


def _has_cjk(s):
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', s or ""))


_TRANSLATE_CACHE = {"model": None, "probed": False}

def _ollama_model_names():
    try:
        return [m["name"] for m in (_http_json(_embed_url() + "/api/tags", timeout=8).get("models") or [])]
    except Exception:
        return []

def _translate_model():
    """跨语检索桥接:选一个本地翻译模型(默认 translategemma:4b)。
    没有就返回 '',search 走原行为(零回归)。结果缓存避免每次检索都探 ollama。
    显式设了 VITUAL_TRANSLATE_MODEL 但该模型未装 → 返回 '' 不回退;
    仅当完全未设 env 才默认 translategemma(便于关闭做 A/B)。"""
    if _TRANSLATE_CACHE["probed"]:
        return _TRANSLATE_CACHE["model"] or ""
    _TRANSLATE_CACHE["probed"] = True
    env = os.environ.get("VITUAL_TRANSLATE_MODEL", "").strip()
    have = _ollama_model_names()
    if env:
        hit = next((c for c in have if c == env or c.split(":")[0] == env.split(":")[0]), None)
        _TRANSLATE_CACHE["model"] = hit or ""      # 显式指定但未装 → 空,不回退
    else:
        hit = next((c for c in have if "translat" in c.lower()), None)
        _TRANSLATE_CACHE["model"] = hit or ""
    return _TRANSLATE_CACHE["model"] or ""

def _translate_to_zh(q):
    model = _translate_model()
    if not model:
        return ""
    try:
        r = _http_json(_embed_url() + "/api/generate", timeout=60,
                       payload={"model": model,
                                "prompt": "Translate the following text into Simplified Chinese. "
                                          "Output only the translation, no explanation or quotes.\n\n" + q,
                                "stream": False})
        return (r.get("response") or "").strip().strip("\"' \n")
    except Exception:
        return ""

def _maybe_translate(q):
    """跨语桥接:英文/非中文查询先译中,再中英合并扩展,让强词法 + 稠密混合检索双命中
    (词法对中文文档、稠密对中文查询都更强)。中文查询(含 CJK)原样返回——
    门禁 16 句中文查询不受影响。无翻译模型时返回原查询(零回归)。"""
    if not q or _has_cjk(q) or not _translate_model():
        return q
    zh = _translate_to_zh(q)
    return (zh + " " + q) if zh else q


def search(q="", kind="", tag="", limit=None, offset=0, mode="auto", sort=""):
    """检索:自然语言问句 → 排序后返回,支持分页。
    mode="auto"     有 embedding 模型且素材已建向量 → 稠密+词法混合;
                    否则纯词法加权(与旧行为一致)
    mode="lexical"  强制词法;mode="semantic" 强制语义(无向量时自动回退词法)
    limit=None 表示不分页;offset 在排序之后生效(全局偏移,非页内)。
    sort=""         默认相关性/时间序;q 存在时=相关性,无 q=时间倒序。
                    显式指定 "newest"/"oldest"/"name"/"size" 时覆盖相关性排序
                    (DAM 面板的排序控件;在分页之前生效,全库级排序)。"""
    _t0 = time.perf_counter()
    q = _maybe_translate(q)
    ck = (q, kind, tag, limit, offset, mode, sort)
    if os.environ.get("VITUAL_CACHE_SEARCH"):
        if ck in _SEARCH_CACHE:
            _hit = _SEARCH_CACHE[ck]
            obs.record("search", duration_ms=0.0, ok=True,
                       zero_hit=not _hit, mode=mode, cached=True)
            return _hit
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
    # 指标埋点:检索延迟 + 零命中率(§8 G 维度)。埋点失败绝不影响检索本身。
    obs.record("search", duration_ms=(time.perf_counter() - _t0) * 1000.0,
               ok=True, zero_hit=not rows, mode=mode, cached=False)
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
    调用方应优雅跳过(见 auto_tag_material 的 skipped 状态),不抛错、不阻断检索。
    排序:VITUAL_CHAT_MODEL 优先;其次 qwen* 优于 gemma*(E2E: gemma4:e2b 常吐非法 JSON)。
    """
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
    prefer = (os.environ.get("VITUAL_CHAT_MODEL") or "").strip()
    def _rank(name):
        n = (name or "").lower()
        if prefer and n == prefer.lower():
            return (0, n)
        if n.startswith("qwen"):
            return (1, n)
        if "gemma" in n:
            return (3, n)
        return (2, n)
    out.sort(key=_rank)
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
    "silent": "无声音轨的画面(视频容器无 audio stream)",
    "audio": "纯音轨/音频文件",
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


def _embed_prefixes(model=None):
    """按 embedding 模型选指令前缀( document / query )。

    bge-m3 等多语模型用官方 retrieval 指令(否则稠密检索质量严重下滑——
    实测 bge-m3 套 nomic 的 `search_document:`/`search_query:` 前缀时跨语召回很弱,
    相关文档被靠近质心的通用片压住);nomic-embed-text 等英文单语模型用原约定。
    前缀必须文档端与查询端一致,否则余弦不可比。
    """
    if model is None:
        model = embed_probe().get("model", "")
    if "bge" in (model or "").lower():
        return ("Represent this passage for retrieval: ",
                "Represent this sentence for searching relevant passages: ")
    return ("search_document: ", "search_query: ")


def doc_text(m):
    """把一条素材拼成用于 embedding 的文档文本。

    经验要点(实测:库内两两余弦均值会从 0.76 降到更可分的水平):
    1. **去掉模板词** —— `sp` 每条都有,只会把所有向量拉向同一方向;
       `type:`/`lang:`/`job:` 这类维度标签保留(有区分度)。
    2. **文件名/目录名按下划线连字符拆词** —— `clean_lama_writing.mp4` →
       `clean lama writing`,模型才能对上 lama/clean 这些词。
    3. **外部引用的目录段含阶段语义**(dehardsub/deblur/mosaic/probe),取末几段。
    4. **指令前缀按模型自适应**(见 `_embed_prefixes`):bge-m3 用官方 retrieval 指令,
       nomic-embed-text 用英文单语约定(`search_document:`/`search_query:`)。
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
    doc_p, _ = _embed_prefixes()
    return doc_p + " | ".join(x for x in parts if x)


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
    _, q_p = _embed_prefixes(info["model"])
    qv = embed_texts([q_p + q])
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
    "无声": ["silent", "mute", "silent-video"],
    "无声视频": ["silent"],
    "静音": ["silent", "mute"],
    "静音视频": ["silent"],
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
    "水印": ["watermark", "logo", "delogo"],
    "台标": ["logo", "watermark", "delogo"],
    # ↓ 2026-10-06 第四轮实例测试暴露的跨语缺口(数据驱动:对齐语料真实 token)
    "标志": ["logo", "watermark", "delogo"],            # remove the logo → 去台标成品
    "语音转文本": ["subs", "srt", "asr", "transcribe"], # speech to text(无 subtitle 词时)
    "转录": ["subs", "srt", "asr", "transcribe"],
    "精简": ["trim", "cut", "clip", "segment"],         # trim intro/outro → 片段
    "上色": ["colorize", "color", "codeformer", "fixed"], # 中文手写 上色
    "彩色": ["color", "colorize", "codeformer", "fixed"], # grayscale to color(译中:彩色)
    "灰度": ["grayscale", "gray", "colorize", "codeformer", "fixed"],
    # ↓ 2026-10-06 第六轮实例测试(缩写/方言/长句)暴露的缺口(数据驱动)
    "放大": ["upscale", "esrgan", "deblur"],            # esrgan x4 upscale → 去模糊成品
    "动漫": ["anime", "cartoon", "deblur", "old_sttn"], # 老动画太糊了 → deblur/STTN
    "动画": ["anime", "cartoon", "deblur", "old_sttn"],
    "画质": ["quality", "cmp", "compare"],              # 画质对比 → 前后对比图
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
    """更新素材的权威标签列(tags)。**整体替换**语义(调用方需自备合并结果)。

    Agent/MCP 打标请用 ``merge_material_tags``(只增不删 + 系统面标签护栏)。

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


# 系统面标签(方案 A / bridge):Agent 合并打标时永不可删。
# E2E 教训:7B 只传「美食,料理」做整体替换 → job:/role: 被抹掉,体检与关系链断裂。
_SYSTEM_TAG_EXACT = frozenset({
    "sp", "demux", "asr", "bilibili", "youtube", "upload",
})
_SYSTEM_TAG_PREFIXES = (
    "role:", "has_audio:", "parent:", "from:", "t_start:", "t_end:",
    "job:", "type:", "lang:",
)


def is_system_facet_tag(tag):
    """是否系统/DAM 面标签(合并护栏保护对象)。"""
    t = (tag or "").strip()
    if not t:
        return False
    if t in _SYSTEM_TAG_EXACT:
        return True
    return any(t.startswith(p) for p in _SYSTEM_TAG_PREFIXES)


# ---------- 标签人读文案(面板 / 卡片;过滤值仍用原始 tag) ----------
# 与 README 受控词表对齐:系统面给中文名,内容标签原样展示。
_TAG_EXACT_LABEL_ZH = {
    "sp": "来源·流水线",
    "upload": "来源·上传",
    "bilibili": "平台·B站",
    "youtube": "平台·YouTube",
    "demux": "拆条产物",
    "asr": "ASR 音轨",
}
_TAG_EXACT_LABEL_EN = {
    "sp": "src·pipeline",
    "upload": "src·upload",
    "bilibili": "platform·Bilibili",
    "youtube": "platform·YouTube",
    "demux": "demuxed",
    "asr": "ASR audio",
}
_TAG_TYPE_LABEL_ZH = {
    "media": "源片",
    "clip": "物理切片",
    "subs": "字幕",
    "notes": "笔记",
    "benchmark": "基准样例",
    "render": "渲染预览",
    "test": "测试样例",
    "batch": "任务杂项",  # 旧值,新入库不再写
    "other": "其它",
}
_TAG_TYPE_LABEL_EN = {
    "media": "source media",
    "clip": "export clip",
    "subs": "captions",
    "notes": "notes",
    "benchmark": "benchmark",
    "render": "render",
    "test": "test",
    "batch": "job misc",
    "other": "other",
}
_TAG_ROLE_LABEL_ZH = {
    "master": "母版",
    "clip": "物理切片",
    "silent-picture": "无声画面",
    "audio-stem": "音轨组件",
    "picture": "画面轨",
}
_TAG_ROLE_LABEL_EN = {
    "master": "master",
    "clip": "export clip",
    "silent-picture": "silent picture",
    "audio-stem": "audio stem",
    "picture": "picture track",
}
# 侧栏默认隐藏:与 parent: 同义的旧 from:;时间码噪音;与 kind/role:master 重复的面
_UI_HIDE_TAG_EXACT = frozenset({
    "has_audio:0", "has_audio:1", "role:picture",
})
_UI_HIDE_TAG_PREFIXES = ("from:", "t_start:", "t_end:")


def tag_ui_meta(tag, lang="zh"):
    """单标签面板元数据:{tag,label,group,hint,hide}。lang=zh|en。"""
    t = (tag or "").strip()
    zh = (lang or "zh").lower().startswith("zh")
    exact = _TAG_EXACT_LABEL_ZH if zh else _TAG_EXACT_LABEL_EN
    types = _TAG_TYPE_LABEL_ZH if zh else _TAG_TYPE_LABEL_EN
    roles = _TAG_ROLE_LABEL_ZH if zh else _TAG_ROLE_LABEL_EN
    hide = t in _UI_HIDE_TAG_EXACT or any(t.startswith(p) for p in _UI_HIDE_TAG_PREFIXES)
    group, label, hint = "content", t, t
    if t in exact:
        group, label = "source", exact[t]
        hint = t
    elif t.startswith("type:"):
        v = t[5:]
        group = "type"
        label = (("类型·" if zh else "type·") + types.get(v, v))
        hint = t
    elif t.startswith("role:"):
        v = t[5:]
        group = "role"
        label = (("角色·" if zh else "role·") + roles.get(v, v))
        hint = t
    elif t.startswith("job:"):
        v = t[4:]
        short = v if len(v) <= 10 else (v[:8] + "…")
        group = "job"
        label = (("任务·" if zh else "job·") + short)
        hint = t
    elif t.startswith("parent:"):
        v = t[7:]
        short = v if len(v) <= 10 else (v[:8] + "…")
        group = "rel"
        label = (("父素材·" if zh else "parent·") + short)
        hint = t
    elif t.startswith("lang:"):
        group = "lang"
        label = (("语言·" if zh else "lang·") + t[5:])
        hint = t
    elif t.startswith("has_audio:"):
        group = "tech"
        label = ("有声" if t.endswith(":1") else "无声") if zh else t
        hint = t
    return {"tag": t, "label": label, "group": group, "hint": hint, "hide": hide}


def scrub_deprecated_from_tags(*, limit=0):
    """去掉与 parent: 重复的旧 from: 别名(库内治理,不经 Agent remove 护栏)。"""
    scanned = cleaned = 0
    for m in all_materials():
        if limit and scanned >= limit:
            break
        scanned += 1
        parts = [t.strip() for t in (m.get("tags") or "").split(",") if t.strip()]
        parents = {t[7:] for t in parts if t.startswith("parent:")}
        if not parents:
            continue
        new_parts = [
            t for t in parts
            if not (t.startswith("from:") and t[5:] in parents)
        ]
        if new_parts == parts:
            continue
        _update_material(m["id"], tags=",".join(new_parts))
        cleaned += 1
    return {"scanned": scanned, "cleaned": cleaned}


def tags_for_ui(*, limit=48, lang="zh"):
    """面板标签云:带人读 label,隐藏冗余系统面,按 count 倒序。"""
    out = []
    for t, c in distinct_tags():
        meta = tag_ui_meta(t, lang=lang)
        if meta["hide"]:
            continue
        out.append({
            "tag": t, "count": c,
            "label": meta["label"], "group": meta["group"], "hint": meta["hint"],
        })
        if limit and len(out) >= limit:
            break
    return out


def _normalize_tag_list(tags):
    if tags is None:
        return []
    if isinstance(tags, (list, tuple, set)):
        return [str(t).strip() for t in tags if str(t).strip()]
    return [t.strip() for t in str(tags).split(",") if t.strip()]


def merge_material_tags(mid, tags="", remove=None, *, purge_ai_tags=True):
    """合并打标:保留已有 → 追加 tags → 仅 remove 可删,且**系统面标签永不删**。

    返回 update_tags 字段 + merged / protected_skipped / removed_applied。
    """
    m = get_material(mid)
    if not m:
        return {"ok": False, "error": "not found: " + str(mid)}
    cur = [t.strip() for t in (m.get("tags") or "").split(",") if t.strip()]
    add = _normalize_tag_list(tags)
    want_remove = set(_normalize_tag_list(remove))
    protected_skipped = sorted(t for t in want_remove if is_system_facet_tag(t))
    removable = {t for t in want_remove if not is_system_facet_tag(t)}
    merged = [t for t in cur if t not in removable]
    for t in add:
        if t and t not in merged:
            merged.append(t)
    new_csv = ",".join(merged)
    r = update_tags(mid, new_csv, purge_ai_tags=purge_ai_tags)
    r["merged"] = new_csv != (m.get("tags") or "")
    r["protected_skipped"] = protected_skipped
    r["removed_applied"] = sorted(removable & set(cur))
    return r


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
    tags = _merge_tag_csv("", media_facet_tags(dest if os.path.isfile(dest) else src, kind))
    add_material(
        id=mid, kind=kind, ext=os.path.splitext(name)[1].lower(), name=name,
        rel_path=os.path.relpath(dest, HUB), size=os.path.getsize(dest), sha256=sha,
        tags=tags, description="", source=source, orig_name=os.path.basename(src),
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    return {"status": "added", "id": mid, "path": os.path.relpath(dest, HUB)}


def _material_abs_path(m):
    p = m.get("external_path") or ""
    if m.get("location") == "external" and p:
        return p
    rp = m.get("rel_path") or ""
    if not rp:
        return ""
    return rp if os.path.isabs(rp) else os.path.join(HUB, rp)


def split_video_to_silent_and_audio(mid, *, force=False):
    """把库里一条有声视频拆成：无声画面(silent) + 音轨(audio)。

    原 videos 条目保留。无声 = 无 audio stream 的 mp4（-an / -map 0:v）。
    物理 clip(role:clip) 默认跳过(切条应在母版上做)。
    返回 {status, silent_id?, audio_id?, silent_path?, audio_path?, reason?}。
    """
    m = get_material(mid)
    if not m:
        return {"status": "error", "reason": "not_found"}
    if m.get("kind") not in ("videos", "silent"):
        return {"status": "skipped", "reason": f"kind={m.get('kind')}"}
    if is_role_clip(m) and not force:
        return {"status": "skipped", "reason": "role:clip"}
    src = _material_abs_path(m)
    if not src or not os.path.isfile(src):
        return {"status": "error", "reason": "missing_file"}
    if m.get("kind") == "silent" and not force:
        return {"status": "skipped", "reason": "already_silent"}
    if m.get("kind") == "videos" and not probe_has_audio(src) and not force:
        # 已无音轨：直接改 kind 即可，不必再拆
        _update_material(
            mid,
            kind="silent",
            tags=_merge_tag_csv(m.get("tags"), media_facet_tags(src, "silent")),
        )
        return {"status": "reclassified", "id": mid, "kind": "silent"}

    ff = ffmpeg_path()
    if not ff:
        return {"status": "error", "reason": "no_ffmpeg"}
    flags = 0x08000000 if os.name == "nt" else 0
    stem = os.path.splitext(sanitize_name(m.get("name") or os.path.basename(src)))[0]
    silent_dir = os.path.join(MATERIALS, "silent")
    audio_dir = os.path.join(MATERIALS, "audio")
    os.makedirs(silent_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)
    silent_path = _unique_dest(silent_dir, f"{stem}_silent.mp4")
    audio_path = _unique_dest(audio_dir, f"{stem}_track.wav")

    try:
        subprocess.run(
            [ff, "-y", "-i", src, "-map", "0:v:0", "-c:v", "copy", "-an", silent_path],
            check=True, capture_output=True, creationflags=flags, timeout=600,
        )
    except Exception as e:
        return {"status": "error", "reason": f"silent_demux:{type(e).__name__}:{e}"}

    audio_ok = False
    if probe_has_audio(src):
        try:
            subprocess.run(
                [
                    ff, "-y", "-i", src, "-vn",
                    "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
                    audio_path,
                ],
                check=True, capture_output=True, creationflags=flags, timeout=600,
            )
            audio_ok = os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0
        except Exception:
            audio_ok = False

    out = {"status": "ok", "source_id": mid}
    # Register silent (already under materials/silent → don't copy again)
    sha_s = compute_sha256(silent_path)
    con = _con()
    ex_s = con.execute("SELECT id FROM materials WHERE sha256=?", (sha_s,)).fetchone()
    con.close()
    if ex_s:
        out["silent_id"] = ex_s[0]
        out["silent_status"] = "duplicate"
    else:
        sid = sha_s[:12]
        add_material(
            id=sid, kind="silent", ext=".mp4",
            name=os.path.basename(silent_path),
            rel_path=os.path.relpath(silent_path, HUB),
            size=os.path.getsize(silent_path), sha256=sha_s,
            tags=_merge_tag_csv(
                "sp,demux," + ",".join(relation_tags(mid) + inherit_parent_context_tags(mid)),
                media_facet_tags(silent_path, "silent"),
            ),
            description=(m.get("description") or "").strip() or f"无声画面 ← {m.get('name')}",
            source="demux-silent", orig_name=os.path.basename(silent_path),
            created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        out["silent_id"] = sid
        out["silent_status"] = "added"
    out["silent_path"] = silent_path

    if audio_ok:
        sha_a = compute_sha256(audio_path)
        con = _con()
        ex_a = con.execute("SELECT id FROM materials WHERE sha256=?", (sha_a,)).fetchone()
        con.close()
        if ex_a:
            out["audio_id"] = ex_a[0]
            out["audio_status"] = "duplicate"
        else:
            aid = sha_a[:12]
            add_material(
                id=aid, kind="audio", ext=".wav",
                name=os.path.basename(audio_path),
                rel_path=os.path.relpath(audio_path, HUB),
                size=os.path.getsize(audio_path), sha256=sha_a,
                tags=_merge_tag_csv(
                    "sp,demux,role:audio-stem,has_audio:1,"
                    + ",".join(relation_tags(mid) + inherit_parent_context_tags(mid)),
                    [],
                ),
                description=f"音轨 ← {m.get('name')}",
                source="demux-audio", orig_name=os.path.basename(audio_path),
                created_at=datetime.datetime.now().isoformat(timespec="seconds"),
            )
            out["audio_id"] = aid
            out["audio_status"] = "added"
        out["audio_path"] = audio_path
    elif os.path.isfile(audio_path):
        try:
            os.remove(audio_path)
        except OSError:
            pass

    # 拆条后给母版补 role:master(幂等)
    cur = get_material(mid)
    if cur and cur.get("kind") == "videos":
        _update_material(
            mid,
            tags=_merge_tag_csv(
                cur.get("tags"),
                ["role:master", "role:picture", "has_audio:1"],
            ),
        )

    return out


def split_all_videos_to_silent(*, force=False, limit=0):
    """对库内全部 videos 拆无声画面 + 音轨(跳过 role:clip)。"""
    ms = [
        m for m in all_materials()
        if m.get("kind") == "videos" and (force or not is_role_clip(m))
    ]
    results = []
    for i, m in enumerate(ms):
        if limit and i >= limit:
            break
        results.append(split_video_to_silent_and_audio(m["id"], force=force))
    return {
        "total": len(ms),
        "ran": len(results),
        "ok": sum(1 for r in results if r.get("status") in ("ok", "reclassified")),
        "results": results,
    }


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
    避免重复占盘。按 sha256 去重;重复时合并 tags(并补空 description)。"""
    src = os.path.abspath(src)
    if not external_path_allowed(src):
        return {"status": "rejected",
                "reason": "external_path outside allowed roots "
                          "(set VITUAL_HUB_EXT_ROOTS to widen)"}
    if not os.path.exists(src) or os.path.isdir(src):
        return None
    base = os.path.basename(src)
    if base.startswith("_probe"):
        return {"status": "skipped", "reason": "probe_stub"}
    sha = compute_sha256(src)
    con = _con()
    con.row_factory = sqlite3.Row
    want_kind = kind or classify(src)
    incoming = [t.strip() for t in (tags or "").split(",") if t.strip()]
    incoming.extend(media_facet_tags(src, want_kind))
    by_path = con.execute(
        "SELECT * FROM materials WHERE location='external' AND external_path=?",
        (src,),
    ).fetchone()
    if by_path:
        mid = by_path["id"]
        fields = {
            "kind": want_kind,
            "tags": _merge_tag_csv(by_path["tags"], incoming),
        }
        if by_path["sha256"] != sha:
            fields.update({
                "sha256": sha,
                "size": os.path.getsize(src),
                "name": sanitize_name(os.path.basename(src)),
                "ext": os.path.splitext(src)[1].lower(),
                "rel_path": src,
            })
        if description:
            fields["description"] = description
        if source:
            fields["source"] = source
        con.close()
        _update_material(mid, **fields)
        return {
            "status": "updated" if by_path["sha256"] != sha else "duplicate",
            "id": mid,
            "merged": True,
        }
    ex = con.execute("SELECT * FROM materials WHERE sha256=?", (sha,)).fetchone()
    if ex:
        mid = ex["id"]
        fields = {}
        merged_tags = _merge_tag_csv(ex["tags"], incoming)
        if merged_tags != (ex["tags"] or ""):
            fields["tags"] = merged_tags
        if (ex["kind"] or "") != want_kind:
            fields["kind"] = want_kind
        if description and not (ex["description"] or "").strip():
            fields["description"] = description
        if source and not (ex["source"] or "").strip():
            fields["source"] = source
        if (ex["external_path"] or "") != src and (ex["location"] or "") == "external":
            fields["external_path"] = src
            fields["rel_path"] = src
        con.close()
        if fields:
            _update_material(mid, **fields)
        return {"status": "duplicate", "id": mid, "merged": bool(fields)}
    name = sanitize_name(os.path.basename(src))
    mid = sha[:12]
    con.execute(
        """INSERT OR REPLACE INTO materials
           (id,kind,ext,name,rel_path,size,sha256,tags,description,source,orig_name,created_at,location,external_path)
           VALUES(:id,:kind,:ext,:name,:rel_path,:size,:sha256,:tags,:description,:source,:orig_name,:created_at,'external',:external_path)""",
        dict(id=mid, kind=want_kind, ext=os.path.splitext(name)[1].lower(), name=name,
             rel_path=src, size=os.path.getsize(src), sha256=sha,
             tags=_merge_tag_csv("", incoming),
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
    videos = kinds.get("videos", 0) + kinds.get("silent", 0)
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


# ----------------------------------------------------------------------------
# #9 按需交付(deliver):把素材包 manifest / 指定 id 导出为下游可用变体
# ----------------------------------------------------------------------------
_FMT_MAP = {"mp4": "mp4", "mov": "mov", "webm": "webm", "mkv": "matroska",
            "gif": "gif"}


def _build_deliver_cmd(exe, src, dst, *, fmt="mp4", res="720", clip=None,
                       copy_only=False):
    """拼装 ffmpeg 交付命令(纯参数构造,不执行)。"""
    cmd = [exe, "-v", "error"]
    if clip:
        start, end = float(clip[0]), float(clip[1])
        cmd += ["-ss", "%.3f" % start, "-i", src,
                "-t", "%.3f" % max(0.0, end - start)]
    else:
        cmd += ["-i", src]
    # 编码策略:区间裁剪需重编码;指定分辨率需重编码;否则可选流拷贝(最快)
    if copy_only and clip is None:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-c:a", "aac"]
        try:
            rh = int(res)
        except (TypeError, ValueError):
            rh = 0
        if rh > 0:
            cmd += ["-vf", "scale=-2:%d" % rh]
    cmd += ["-f", _FMT_MAP.get(fmt, fmt), dst]
    return cmd


def deliver_package(manifest_path=None, ids=None, *, out_dir=None,
                    confirm=False, fmt="mp4", res="720",
                    clips=None, copy_only=False, overwrite=False):
    """#9 按需交付:把素材包 manifest 或指定 id 列表导出为下游可用变体。

    安全铁律:只读原素材、只新建交付文件(绝不改动资产本体),落到 out_dir
    (默认 ``index/agent_workspace/deliveries/<时间戳>/``,派生数据,可删可重建)。
    ``confirm=False``(默认)仅返回 dry-run 计划、不写任何文件;``confirm=True``
    才真正调用 ffmpeg 导出。

    Args:
      manifest_path: 素材包 manifest JSON 路径(与 ids 二选一)
      ids: 素材 id 列表(字符串)
      out_dir: 交付目录(默认上述 deliveries 子目录)
      confirm: 是否执行导出(写文件);False=dry-run
      fmt: 目标封装(mp4/mov/webm/mkv/gif)
      res: 目标高度像素(720/1080;0=保持原分辨率)
      clips: 可选区间裁剪 {id: (start, end)}(秒)
      copy_only: True=流拷贝不重编码(仅 remux;res 忽略;clip 仍走重编码)
      overwrite: 目标已存在是否覆盖(否则跳过并标 skipped)

    交付策略按 kind 分流:**视频类**(videos/silent/anim)走 ffmpeg 转码/裁剪;
    **非视频类**(docs/subs/audio/images)按原样复制、保留原扩展名,不做转码
    (实例教训:对 .srt/.ass 硬转 mp4 必然失败)。
    Returns: dict(dry_run, out_dir, plan[], written[], skipped[], errors[])
    """
    target_ids = []
    if manifest_path:
        try:
            with open(manifest_path, encoding="utf-8") as f:
                man = json.load(f)
            for a in (man.get("assets") or []):
                i = a.get("id")
                if i and str(i) not in target_ids:
                    target_ids.append(str(i))
        except Exception as e:  # noqa: BLE001
            return {"error": "manifest read failed: %s" % e,
                    "dry_run": not confirm, "out_dir": out_dir or "",
                    "plan": [], "written": [], "skipped": [], "errors": []}
    for i in (ids or []):
        s = str(i).strip()
        if s and s not in target_ids:
            target_ids.append(s)

    if not target_ids:
        return {"error": "no target ids (provide manifest_path or ids)",
                "dry_run": not confirm, "out_dir": out_dir or "",
                "plan": [], "written": [], "skipped": [], "errors": []}

    exe = ffmpeg_path()
    if out_dir:
        out_dir = os.path.abspath(out_dir)
    else:
        out_dir = os.path.join(HUB, "index", "agent_workspace",
                               "deliveries", time.strftime("%Y%m%d_%H%M%S"))

    plan, written, skipped, errors = [], [], [], []
    for mid in target_ids:
        m = get_material(mid)
        if not m:
            errors.append({"id": mid, "status": "not_found"})
            plan.append({"id": mid, "status": "not_found"})
            continue
        src = _abs_source(m)
        if not src or not os.path.exists(src):
            errors.append({"id": mid, "status": "source_missing", "src": src})
            plan.append({"id": mid, "status": "source_missing", "src": src})
            continue
        clip = (clips or {}).get(mid)
        nm = re.sub(r"\W+", "_", (m.get("name") or mid))[:40].strip("_") or mid
        # 非视频类(文档/字幕/音频/图片)不强行转码:按原样复制交付并保留原扩展名。
        # 实例教训:对 .srt/.ass 硬转 mp4 必然失败(ffmpeg 无法把字幕当视频编码)。
        video_like = m.get("kind") in VIDEO_LIKE_KINDS
        if video_like:
            dst = os.path.join(out_dir, "%s_%s.%s" % (mid, nm, fmt))
            cmd = _build_deliver_cmd(exe, src, dst, fmt=fmt, res=res,
                                     clip=clip, copy_only=copy_only)
        else:
            ext = os.path.splitext(src)[1].lstrip(".") or "bin"
            dst = os.path.join(out_dir, "%s_%s.%s" % (mid, nm, ext))
            cmd = ["copy", src, dst]
        entry = {"id": mid, "src": src, "dst": dst, "cmd": cmd,
                 "clip": list(clip) if clip else None,
                 "mode": "transcode" if video_like else "copy",
                 "status": "planned"}
        plan.append(entry)
        if not confirm:
            continue
        if os.path.exists(dst) and not overwrite:
            skipped.append({"id": mid, "dst": dst, "status": "exists"})
            entry["status"] = "skipped_exists"
            continue
        os.makedirs(out_dir, exist_ok=True)
        if not video_like:
            try:
                shutil.copy2(src, dst)
                written.append({"id": mid, "dst": dst, "status": "copied"})
                entry["status"] = "copied"
            except Exception as e:  # noqa: BLE001
                errors.append({"id": mid, "status": "copy_error",
                               "detail": str(e)[:200]})
                entry["status"] = "copy_error"
            continue
        if exe is None:
            errors.append({"id": mid, "status": "no_ffmpeg"})
            entry["status"] = "no_ffmpeg"
            continue
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=600,
                               creationflags=flags)
            if p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                written.append({"id": mid, "dst": dst, "status": "ok"})
                entry["status"] = "ok"
            else:
                err = (p.stderr or b"").decode("utf-8", "ignore")[:300]
                errors.append({"id": mid, "status": "ffmpeg_error", "detail": err})
                entry["status"] = "ffmpeg_error"
        except Exception as e:  # noqa: BLE001
            errors.append({"id": mid, "status": "exception", "detail": str(e)[:200]})
            entry["status"] = "exception"

    return {"dry_run": not confirm, "out_dir": out_dir, "plan": plan,
            "written": written, "skipped": skipped, "errors": errors}


def make_thumb(mid, retry_failed=False):
    """为视频抽一帧存成 jpg 封面,缓存到 index/thumbs/<id>.jpg。
    返回封面路径;无 ffmpeg / 非视频 / 抽帧失败均返回 None。

    抽帧失败的会留下 `<id>.jpg.fail` 标记:源文件损坏/未写完时 ffmpeg 很费时,
    标记后不再反复重试(想重试:purge_thumbs() 清标记,或传 retry_failed=True)。"""
    m = get_material(mid)
    if not m or m["kind"] not in VIDEO_LIKE_KINDS:
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
    if m.get("kind") not in VISUAL_KINDS:
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
    ms = [m for m in all_materials() if m.get("kind") in VISUAL_KINDS]
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


# 主阈值 + 过稀/过密再调。注意:showinfo 必须在 -v info 才有 pts_time;
# -v warning/error 会吞掉 Parsed_showinfo(实测 0 切点 → 误走 interval)。
_SHOT_PRIMARY = 0.30
_SHOT_FALLBACK_LOW = (0.20, 0.15)     # 主档 0 切点再放宽
_SHOT_FALLBACK_HIGH = (0.40, 0.50)    # 切点过多再收紧


def _ffmpeg_scene_cuts(exe, src, threshold):
    """跑一遍 ffmpeg 场景检测,返回切点列表(秒,升序去重)。

    用 select='gt(scene,t)' 过滤镜头切换帧,showinfo 把被选中的帧打印到 stderr,
    从 stderr 解析 `pts_time:([0-9.]+)` 即切点。子进程失败返回 None(调用方报 error)。
    日志级别必须是 info:warning 会丢掉 showinfo 行。"""
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW,避免弹黑框
    try:
        p = subprocess.run(
            [exe, "-v", "info", "-i", src,
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=300, creationflags=flags)
    except Exception:
        return None
    txt = (p.stderr or b"").decode("utf-8", "ignore")
    cuts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", txt)]
    return sorted(set(cuts))


def _thin_cuts(cuts, max_n):
    """切点仍多于 max_n 时等间隔抽样,保留首尾感(下标均匀)。"""
    if max_n <= 0 or len(cuts) <= max_n:
        return cuts
    if max_n == 1:
        return [cuts[len(cuts) // 2]]
    step = (len(cuts) - 1) / float(max_n - 1)
    idxs = sorted({int(round(i * step)) for i in range(max_n)})
    return [cuts[i] for i in idxs]


def build_shot_index(mid, force=False, max_scenes=60):
    """为视频建立镜头索引:ffmpeg 场景检测 → 切点 → 片段级 start/end 时间轴,
    落 sidecar `index/shots/<id>.json`。幂等:已有 sidecar 且未 force → cached。
    变更记入 history 审计。返回 dict,状态 ∈ ok|cached|skipped|error。"""
    m = get_material(mid)
    if not m:
        return {"id": mid, "status": "error", "reason": "not_found"}
    if m.get("kind") not in VIDEO_LIKE_KINDS:
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
    # 主档一次;0 切点再放宽;过多再收紧;仍过多则 thin(少跑全片)
    cuts = _ffmpeg_scene_cuts(exe, src, _SHOT_PRIMARY)
    if cuts is None:
        return {"id": mid, "status": "error", "reason": "ffmpeg_failed"}
    thr = _SHOT_PRIMARY
    if not cuts:
        for t in _SHOT_FALLBACK_LOW:
            c = _ffmpeg_scene_cuts(exe, src, t)
            if c is None:
                return {"id": mid, "status": "error", "reason": "ffmpeg_failed"}
            if c:
                cuts, thr = c, t
                break
    elif len(cuts) > max_scenes:
        for t in _SHOT_FALLBACK_HIGH:
            c = _ffmpeg_scene_cuts(exe, src, t)
            if c is None:
                return {"id": mid, "status": "error", "reason": "ffmpeg_failed"}
            cuts, thr = c, t
            if len(cuts) <= max_scenes:
                break
    if len(cuts) > max_scenes:
        cuts = _thin_cuts(cuts, max_scenes)
        thr = "%s+thin%d" % (thr, max_scenes)
    # 场景检测全空(长镜头/渐变):按等间隔切逻辑段,供 Agent 按时间窗调用
    if not cuts and dur and dur > 12:
        step = max(8.0, min(20.0, dur / 10.0))
        t = step
        while t < dur - 2.0:
            cuts.append(round(t, 3))
            t += step
        thr = "interval:%.1f" % step
    last = max(dur, cuts[-1] if cuts else 0.0)
    bounds = [0.0] + [c for c in cuts if 0.0 < c < last] + [last]
    scenes = [{"start": round(a, 3), "end": round(b, 3)}
              for a, b in zip(bounds, bounds[1:]) if b > a]
    if not scenes:
        if last > 0:
            scenes = [{"start": 0.0, "end": round(last, 3)}]
        else:
            # 取不到时长(dur<=0)且无切点:合成 0 秒 scene 会误导 job_checkup 误判母版
            # "有镜头",直接报错而非写假 sidecar。
            return {"id": mid, "status": "error", "reason": "no_duration"}
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(sc, "w", encoding="utf-8") as f:
        json.dump({"duration": dur, "threshold": thr, "scenes": scenes},
                  f, ensure_ascii=False)
    log_history("app", "shot_index", mid, f"scenes={len(scenes)}")
    return {"id": mid, "status": "ok", "scenes": len(scenes), "duration": dur,
            "threshold": thr, "cuts": len(cuts)}


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
    ms = [m for m in all_materials() if m.get("kind") in VIDEO_LIKE_KINDS]
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
    if m.get("kind") not in VISUAL_KINDS:
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
    ms = [m for m in all_materials() if m.get("kind") in VISUAL_KINDS]
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


def _load_all_phashes():
    """读全部 phash sidecar → [(id, bits_int), ...]。坏文件跳过。"""
    out = []
    if not os.path.isdir(PHASH_DIR):
        return out
    for fn in os.listdir(PHASH_DIR):
        if not fn.endswith(".txt"):
            continue
        mid = fn[:-len(".txt")]
        if not mid or any(c in mid for c in "\\/:"):
            continue
        try:
            with open(os.path.join(PHASH_DIR, fn), "r", encoding="utf-8") as f:
                out.append((mid, int(f.read().strip(), 16)))
        except (OSError, ValueError):
            continue
    return out


def near_duplicate_report(max_dist=10, limit_pairs=50):
    """全库画面近重复报告(只读):扫全部 dHash sidecar,列出汉明距离≤max_dist 的对,
    并用并查集归成「一实体多引用」簇。

    返回:
      hashed      — 参与比较的 sidecar 数
      pairs       — [{a,b,dist,a_name,b_name},...] 按 dist 升序,最多 limit_pairs
      clusters    — [{ids,size,names},...] size≥2,按 size 降序
      pair_count  — 未截断前的近重复对数
    """
    try:
        max_dist = int(max_dist)
    except (TypeError, ValueError):
        max_dist = 10
    try:
        limit_pairs = int(limit_pairs)
    except (TypeError, ValueError):
        limit_pairs = 50
    items = _load_all_phashes()
    parent = {mid: mid for mid, _ in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    pairs = []
    n = len(items)
    for i in range(n):
        mid_a, ha = items[i]
        for j in range(i + 1, n):
            mid_b, hb = items[j]
            d = hamming(ha, hb)
            if d <= max_dist:
                union(mid_a, mid_b)
                ma = get_material(mid_a) or {}
                mb = get_material(mid_b) or {}
                pairs.append({
                    "a": mid_a, "b": mid_b, "dist": d,
                    "a_name": ma.get("name", ""), "b_name": mb.get("name", ""),
                    "a_kind": ma.get("kind", ""), "b_kind": mb.get("kind", ""),
                })
    pairs.sort(key=lambda x: (x["dist"], x["a"], x["b"]))
    pair_count = len(pairs)
    # 并查集聚簇
    buckets = {}
    for mid, _ in items:
        buckets.setdefault(find(mid), []).append(mid)
    clusters = []
    for ids in buckets.values():
        if len(ids) < 2:
            continue
        ids = sorted(ids)
        names = [(get_material(i) or {}).get("name", "") for i in ids]
        clusters.append({"ids": ids, "size": len(ids), "names": names})
    clusters.sort(key=lambda c: (-c["size"], c["ids"][0]))
    return {
        "hashed": n,
        "max_dist": max_dist,
        "pair_count": pair_count,
        "pairs": pairs[: max(0, limit_pairs)],
        "clusters": clusters,
        "cluster_count": len(clusters),
    }


def _clip_python():
    """承载 open_clip 的 python:优先 VITUAL_CLIP_PYTHON,否则与 OCR 同用 SP venv。"""
    env = os.environ.get("VITUAL_CLIP_PYTHON", "").strip()
    if env and os.path.isfile(env):
        return env
    return _ocr_python()


def _clip_cache_dir():
    d = (os.environ.get("VITUAL_CLIP_CACHE") or os.environ.get("OPEN_CLIP_CACHE_DIR")
         or "").strip()
    return d or os.path.join(HUB, "models", "clip")


def _clip_runner_cmd(args, timeout=600):
    """调 clip_runner.py;返回解析后的 dict。失败 → {"error": ...}。"""
    py = _clip_python()
    runner = os.path.join(HUB, "clip_runner.py")
    if not py or not os.path.isfile(runner):
        return {"error": "no_clip_python"}
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("VITUAL_CLIP_CACHE", _clip_cache_dir())
    env.setdefault("OPEN_CLIP_CACHE_DIR", env["VITUAL_CLIP_CACHE"])
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        p = subprocess.run(
            [py, runner] + list(args),
            capture_output=True, timeout=timeout, env=env,
            creationflags=flags,
        )
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    raw = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not raw:
        err = (p.stderr or b"").decode("utf-8", "replace")[:300]
        return {"error": "empty_stdout:" + err}
    try:
        return json.loads(raw.splitlines()[-1])
    except Exception as e:
        return {"error": "bad_json:%s" % e, "raw": raw[:200]}


_CLIP_PROBE_CACHE = {"probed": False, "info": None}


def clip_probe(refresh=False):
    """探测 CLIP 后端优先级:
      1) 本地 open_clip(SP venv + 已下载权重) → backend=local
      2) VITUAL_CLIP_URL HTTP → backend=http
      否则 ok=False(以图搜图自动降级 dHash)。
    """
    if _CLIP_PROBE_CACHE["probed"] and not refresh:
        return _CLIP_PROBE_CACHE["info"]
    # HTTP 优先仅当显式要求? 否:本地优先(离线友好)
    r = _clip_runner_cmd(["probe"], timeout=120)
    if r.get("ok"):
        info = {"ok": True, "backend": "local", "model": r.get("model", ""),
                "dim": r.get("dim", 0), "device": r.get("device", ""),
                "cache": r.get("cache", ""), "url": "", "err": ""}
        _CLIP_PROBE_CACHE.update(probed=True, info=info)
        return info
    local_err = r.get("error") or r.get("hint") or "local_unavailable"
    url = (os.environ.get("VITUAL_CLIP_URL") or "").strip().rstrip("/")
    if url:
        info = {"ok": True, "backend": "http", "url": url, "model": "http",
                "dim": 0, "err": "", "local_err": local_err}
        _CLIP_PROBE_CACHE.update(probed=True, info=info)
        return info
    info = {"ok": False, "backend": "", "url": "", "model": "",
            "err": local_err, "cache": _clip_cache_dir()}
    _CLIP_PROBE_CACHE.update(probed=True, info=info)
    return info


def _dhash_bits_from_path(path):
    """任意本地图片/视频路径 → 64-bit dHash int;失败 None。"""
    if not path or not os.path.isfile(path):
        return None
    exe = ffmpeg_path()
    if not exe:
        return None
    return _ffmpeg_dhash_bits(exe, path)


def _resolve_image_query(query):
    """把 query(素材 id 或路径)解析为 (bits|None, meta dict)。"""
    q = (query or "").strip().strip('"')
    meta = {"query": q, "source": ""}
    if not q:
        return None, {**meta, "error": "empty_query"}
    sc = phash_path(q)
    if sc and os.path.isfile(sc):
        try:
            with open(sc, "r", encoding="utf-8") as f:
                return int(f.read().strip(), 16), {**meta, "source": "sidecar", "id": q}
        except (OSError, ValueError):
            pass
    m = get_material(q)
    if m:
        src = _abs_source(m)
        bits = _dhash_bits_from_path(src)
        if bits is not None:
            return bits, {**meta, "source": "material", "id": q, "path": src}
        return None, {**meta, "error": "hash_failed", "id": q, "path": src or ""}
    path = os.path.abspath(q) if not os.path.isabs(q) else q
    if os.path.isfile(path):
        if not external_path_allowed(path):
            return None, {**meta, "error": "path_not_allowed", "path": path}
        bits = _dhash_bits_from_path(path)
        if bits is not None:
            return bits, {**meta, "source": "path", "path": path}
        return None, {**meta, "error": "hash_failed", "path": path}
    return None, {**meta, "error": "not_found"}


def _clip_visual_path(m):
    """素材用于 CLIP 的画面路径:优先封面 jpg,否则图片源文件,视频则抽 1 帧到临时。"""
    mid = m["id"]
    tp = thumb_path(mid)
    if tp and os.path.isfile(tp) and os.path.getsize(tp) > 0:
        return tp, "thumb"
    src = _abs_source(m) or ""
    if m.get("kind") == "images" and src and os.path.isfile(src):
        return src, "source"
    if m.get("kind") in VIDEO_LIKE_KINDS and src and os.path.isfile(src):
        # 无封面时临时抽一帧(不污染 thumbs 失败标记)
        exe = ffmpeg_path()
        if not exe:
            return "", "no_ffmpeg"
        os.makedirs(os.path.join(INDEX_DIR, "_clip_frames"), exist_ok=True)
        dst = os.path.join(INDEX_DIR, "_clip_frames", mid + ".jpg")
        if not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
            flags = 0x08000000 if os.name == "nt" else 0
            try:
                subprocess.run(
                    [exe, "-v", "error", "-y", "-i", src, "-frames:v", "1",
                     "-vf", "scale=224:-2", "-q:v", "4", dst],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=60, creationflags=flags,
                )
            except Exception:
                return "", "frame_fail"
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst, "frame"
    return "", "missing"


def _clip_embed_images(paths):
    """批量嵌图。返回 list[list[float]|None]。"""
    if not paths:
        return []
    info = clip_probe()
    if not info["ok"]:
        return [None] * len(paths)
    if info.get("backend") == "http":
        out = []
        for p in paths:
            out.append(_clip_embed_image_http(p))
        return out
    r = _clip_runner_cmd(["embed-image"] + list(paths), timeout=max(120, 30 * len(paths)))
    if r.get("error"):
        return [None] * len(paths)
    vecs = r.get("vectors") or []
    # 对齐长度
    while len(vecs) < len(paths):
        vecs.append(None)
    return vecs[:len(paths)]


def _clip_embed_texts(texts):
    if not texts:
        return []
    info = clip_probe()
    if not info["ok"]:
        return [None] * len(texts)
    if info.get("backend") == "http":
        return [_clip_embed_text_http(t) for t in texts]
    r = _clip_runner_cmd(["embed-text"] + list(texts), timeout=120)
    if r.get("error"):
        return [None] * len(texts)
    vecs = r.get("vectors") or []
    while len(vecs) < len(texts):
        vecs.append(None)
    return vecs[:len(texts)]


def _clip_embed_image_http(path):
    info = clip_probe()
    if not info.get("url"):
        return None
    try:
        import base64
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        r = _http_json(info["url"] + "/embed_image", timeout=60,
                       payload={"image_b64": b64, "path": path})
        vec = r.get("embedding") or r.get("vector")
        if isinstance(vec, list) and vec:
            return [float(x) for x in vec]
    except Exception:
        return None
    return None


def _clip_embed_text_http(text):
    info = clip_probe()
    if not info.get("url") or not (text or "").strip():
        return None
    try:
        r = _http_json(info["url"] + "/embed_text", timeout=30,
                       payload={"text": text})
        vec = r.get("embedding") or r.get("vector")
        if isinstance(vec, list) and vec:
            return [float(x) for x in vec]
    except Exception:
        return None
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return -1.0
    import math
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def image_embed_status():
    """图像 CLIP 索引覆盖率。"""
    info = clip_probe()
    con = _con()
    n = con.execute("SELECT COUNT(*) c FROM image_embeddings").fetchone()[0]
    con.close()
    visuals = sum(1 for m in all_materials() if m.get("kind") in VISUAL_KINDS)
    return {
        "available": bool(info.get("ok")),
        "backend": info.get("backend", ""),
        "model": info.get("model", ""),
        "embedded": n,
        "visual_total": visuals,
        "coverage": round(n / visuals, 3) if visuals else 0.0,
        "err": info.get("err", ""),
    }


def build_image_embeddings(force=False, limit=0, progress=None):
    """为 videos/silent/images 建 CLIP 图像向量(增量)。无后端 → available=False。"""
    info = clip_probe(refresh=True)
    if not info.get("ok"):
        return {"available": False, "embedded": 0, "skipped": 0, "total": 0,
                "err": info.get("err", "clip_unavailable")}
    model = info.get("model") or "clip"
    ms = [m for m in all_materials() if m.get("kind") in VISUAL_KINDS]
    con = _con()
    con.row_factory = sqlite3.Row
    have = {r["mid"]: (r["model"], r["sig"]) for r in con.execute(
        "SELECT mid, model, sig FROM image_embeddings").fetchall()}
    con.close()
    todo = []
    for m in ms:
        path, how = _clip_visual_path(m)
        if not path:
            continue
        try:
            sig = "%s:%d" % (how, os.path.getsize(path))
        except OSError:
            continue
        if not force and have.get(m["id"]) == (model, sig):
            continue
        todo.append((m["id"], path, sig))
    total = len(ms)
    skipped = total - len(todo)
    if limit:
        todo = todo[:limit]
    done = 0
    batch = 8
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = _clip_embed_images([p for _, p, _ in chunk])
        con = _con()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for (mid, path, sig), vec in zip(chunk, vecs):
            if not vec:
                continue
            con.execute(
                "INSERT OR REPLACE INTO image_embeddings(mid,model,dim,sig,vec,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (mid, model, len(vec), sig, _vec_to_blob(vec), now),
            )
            done += 1
        con.commit()
        con.close()
        if progress:
            progress(min(i + batch, len(todo)), len(todo))
    log_history("app", "imgembed", "", "embedded=%d model=%s" % (done, model))
    return {"available": True, "model": model, "total": total,
            "embedded": done, "skipped": skipped, "pending": len(todo) - done}


def _load_image_vectors(model=None):
    con = _con()
    con.row_factory = sqlite3.Row
    if model:
        rows = con.execute(
            "SELECT mid, vec FROM image_embeddings WHERE model=?", (model,)
        ).fetchall()
    else:
        rows = con.execute("SELECT mid, vec FROM image_embeddings").fetchall()
    con.close()
    return {r["mid"]: _blob_to_vec(r["vec"]) for r in rows}


def search_by_clip_vector(qv, *, limit=20, skip_id="", min_score=0.15):
    """给定查询向量,在 image_embeddings 里余弦召回。"""
    if not qv:
        return []
    info = clip_probe()
    model = info.get("model") or None
    store = _load_image_vectors(model if info.get("backend") == "local" else None)
    scored = []
    for mid, iv in store.items():
        if skip_id and mid == skip_id:
            continue
        s = _cosine(qv, iv)
        if s < min_score:
            continue
        m = get_material(mid) or {}
        scored.append({"id": mid, "name": m.get("name", ""),
                       "kind": m.get("kind", ""), "score": round(s, 4)})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def search_by_text_image(text, *, limit=20, min_score=0.15):
    """以文搜图(CLIP 共空间)。无后端/无索引 → status unavailable/empty。"""
    text = (text or "").strip()
    if not text:
        return {"status": "error", "mode": "clip-text", "matches": [],
                "error": "empty_query"}
    info = clip_probe()
    if not info.get("ok"):
        return {"status": "unavailable", "mode": "clip-text", "clip": info,
                "matches": [], "hint": "需 SP venv open_clip + 权重,或 VITUAL_CLIP_URL"}
    st = image_embed_status()
    if st["embedded"] <= 0:
        return {"status": "empty_index", "mode": "clip-text", "matches": [],
                "hint": "先 python cli.py imgembed 建图像索引"}
    vecs = _clip_embed_texts([text])
    if not vecs or not vecs[0]:
        return {"status": "error", "mode": "clip-text", "matches": [],
                "error": "embed_failed"}
    hits = search_by_clip_vector(vecs[0], limit=limit, min_score=min_score)
    return {"status": "ok", "mode": "clip-text", "matches": hits,
            "total": len(hits), "model": info.get("model", "")}


def search_by_image(query, *, max_dist=10, limit=20, mode="auto"):
    """以图搜图。

    mode:
      auto  — 有 CLIP 索引则 clip,否则 phash
      phash — dHash 汉明近邻
      clip  — CLIP 向量;无后端/无索引 → unavailable/empty_index
    """
    mode = (mode or "auto").strip().lower()
    try:
        max_dist = int(max_dist)
        limit = int(limit) or 20
    except (TypeError, ValueError):
        max_dist, limit = 10, 20

    st = image_embed_status()  # 一次探测,auto/clip/phash 三路径共用(去冗余)
    want_clip = mode == "clip"
    if mode == "auto":
        want_clip = bool(st.get("available") and st.get("embedded", 0) > 0)

    if want_clip or mode == "clip":
        info = clip_probe()
        if not info.get("ok"):
            if mode == "clip":
                return {"status": "unavailable", "mode": "clip", "clip": info,
                        "matches": [],
                        "hint": "本地 open_clip 权重未就绪且无 VITUAL_CLIP_URL;"
                                "可改 mode=phash,或下载权重到 models/clip"}
            want_clip = False
        else:
            if st["embedded"] <= 0 and mode == "clip":
                return {"status": "empty_index", "mode": "clip", "matches": [],
                        "hint": "先 python cli.py imgembed"}
            if st["embedded"] > 0:
                # 解析查询为可嵌图路径
                path, skip_id = "", ""
                m = get_material((query or "").strip())
                if m:
                    skip_id = m["id"]
                    path, _ = _clip_visual_path(m)
                else:
                    cand = os.path.abspath((query or "").strip())
                    if os.path.isfile(cand) and external_path_allowed(cand):
                        path = cand
                if not path:
                    if mode == "clip":
                        return {"status": "error", "mode": "clip", "matches": [],
                                "error": "no_visual_for_query"}
                    want_clip = False
                else:
                    vecs = _clip_embed_images([path])
                    if not vecs or not vecs[0]:
                        if mode == "clip":
                            return {"status": "error", "mode": "clip",
                                    "matches": [], "error": "embed_failed"}
                        want_clip = False
                    else:
                        hits = search_by_clip_vector(
                            vecs[0], limit=limit, skip_id=skip_id)
                        return {"status": "ok", "mode": "clip",
                                "matches": hits, "total": len(hits),
                                "model": info.get("model", ""),
                                "id": skip_id or "", "path": path}

    bits, meta = _resolve_image_query(query)
    if bits is None:
        return {"status": "error", "mode": "phash", "matches": [], **meta}
    skip_id = meta.get("id") or ""
    hits, total = [], 0
    for oid, other in _load_all_phashes():
        if skip_id and oid == skip_id:
            continue
        total += 1
        d = hamming(bits, other)
        if d <= max_dist:
            m = get_material(oid) or {}
            hits.append({"id": oid, "name": m.get("name", ""),
                         "kind": m.get("kind", ""), "dist": d})
    hits.sort(key=lambda x: x["dist"])
    out = {"status": "ok", "mode": "phash", "matches": hits[:limit],
           "total": total, "max_dist": max_dist,
           "clip_ready": bool(clip_probe().get("ok")),
           "clip_embedded": st.get("embedded", 0),
           }
    for k in ("source", "id", "path"):
        if k in meta:
            out[k] = meta[k]
    return out


# ---------- 事件驱动自动处理链(封面→OCR→镜头索引→pHash→自动打标→语义索引) ----------
def pending_processing(mid):
    """返回该素材还缺哪些自动处理步骤(True=缺)。

    判断依据:thumb 看 thumb_path(mid) 的 jpg 是否存在且非空;
    ocr/shots/phash 看各自 sidecar(index/ocr|shots|phash/<id>.*)是否已落盘。
    非 videos/silent/images 素材(docs/subs/audio/other)不需要画面类派生数据 → 全 False。
    物理 clip(role:clip):镜头索引挂在母版上,shots 永不标缺。
    images:不需要 shots(场景检测只对视频)。"""
    m = get_material(mid)
    if not m or m.get("kind") not in VISUAL_KINDS:
        return {"thumb": False, "ocr": False, "shots": False, "phash": False}
    tp = thumb_path(mid)
    op, sp, pp = ocr_sidecar_path(mid), shot_index_path(mid), phash_path(mid)
    kind = m.get("kind")
    # 图片本身可当封面;有文件即不缺 thumb
    if kind == "images":
        src = _material_abs_path(m)
        need_thumb = not (src and os.path.isfile(src))
    else:
        need_thumb = not (tp and os.path.exists(tp) and os.path.getsize(tp) > 0)
    need_shots = kind in ("videos", "silent") and not is_role_clip(m)
    return {
        "thumb": need_thumb,
        "ocr": not (op and os.path.isfile(op)),
        "shots": need_shots and not (sp and os.path.isfile(sp)),
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
    # 物理 clip / 图片 不建镜头表(逻辑片段只在母版 videos/silent shots)
    m0 = get_material(mid) or {}
    run_shots = (
        shots
        and m0.get("kind") in ("videos", "silent")
        and not is_role_clip(m0)
    )
    if run_shots:
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
    """批处理:找出所有还有缺项的 videos/silent/images 素材,逐条跑 auto_process_material,
    最后调一次 build_embeddings()(增量:只补新素材/文本有变化的向量)。
    limit>0 时只处理前 limit 条。返回 {"processed", "results", "embed"}。"""
    ms = [m for m in all_materials()
          if m.get("kind") in VISUAL_KINDS
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
        if m["kind"] not in VIDEO_LIKE_KINDS:
            continue
        p = thumb_path(m["id"])
        if os.path.exists(p) and os.path.getsize(p) > 0:
            continue
        if skip_failed and os.path.exists(p + ".fail"):
            continue
        out.append(m["id"])
    return out


def list_missing_covers(kind="", skip_failed=True, limit=50):
    """无封面的 videos/silent 清单(Agent/MCP 只读技能用)。
    kind 可滤 `silent`/`videos`;空=两者。返回 [{id,kind,name,tags},...]。"""
    want = {kind} if kind in VIDEO_LIKE_KINDS else set(VIDEO_LIKE_KINDS)
    ids = set(missing_thumbnail_ids(skip_failed=skip_failed))
    out = []
    for m in all_materials():
        if m["id"] not in ids or m.get("kind") not in want:
            continue
        out.append({
            "id": m["id"], "kind": m.get("kind", ""), "name": m.get("name", ""),
            "tags": m.get("tags", ""),
        })
        if limit and len(out) >= limit:
            break
    return out


def job_checkup(job_id):
    """某 subtitle_pipeline job 的资产体检(只读,确定性,零幻觉)。

    看 kinds 是否齐(videos/silent/audio/subs/docs)、封面/镜头缺项、asr 音轨标签、
    失效外部引用;并汇总 master/clip/组件关系(方案 A)。
    Agent Prompt「job 体检」与 MCP tool 共用。
    """
    jid = (job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id required"}
    tag = jid if jid.startswith("job:") else ("job:" + jid)
    rows = search("", tag=tag, limit=500) or []
    # 兼容 tag 里只有裸 job id 的旧数据
    if not rows and not jid.startswith("job:"):
        rows = [m for m in all_materials()
                if ("job:" + jid) in (m.get("tags") or "")
                or jid in (m.get("tags") or "").split(",")]
    kinds = {}
    missing_thumbs, missing_shots = [], []
    asr_ids, broken_ids = [], []
    master_ids, clip_ids, component_ids = [], [], []
    clips_missing_parent = []
    for m in rows:
        k = m.get("kind") or "?"
        kinds[k] = kinds.get(k, 0) + 1
        tags = _material_tag_set(m)
        if is_role_master(tags) or (
            k == "videos" and "role:clip" not in tags and "type:clip" not in tags
        ):
            if m["id"] not in master_ids:
                master_ids.append(m["id"])
        if is_role_clip(tags) or "type:clip" in tags:
            clip_ids.append(m["id"])
            if not any(t.startswith("parent:") for t in tags):
                clips_missing_parent.append(m["id"])
        if "role:silent-picture" in tags or "role:audio-stem" in tags or "demux" in tags:
            component_ids.append(m["id"])
        if k in VIDEO_LIKE_KINDS:
            tp = thumb_path(m["id"])
            if not (os.path.exists(tp) and os.path.getsize(tp) > 0):
                if not os.path.exists(tp + ".fail"):
                    missing_thumbs.append(m["id"])
            # 镜头挂母版:物理 clip / demux silent 不要求 shots
            if (
                not is_role_clip(tags)
                and "type:clip" not in tags
                and k == "videos"
                and get_shots(m["id"]) is None
            ):
                missing_shots.append(m["id"])
        if k == "audio" and ("asr" in tags or "16k" in (m.get("name") or "").lower()):
            asr_ids.append(m["id"])
        if m.get("location") == "external":
            ep = m.get("external_path") or ""
            if ep and not os.path.isfile(ep):
                broken_ids.append(m["id"])
    ok = (
        bool(rows)
        and not broken_ids
        and (kinds.get("videos", 0) + kinds.get("silent", 0)) > 0
    )
    return {
        "ok": ok,
        "job": jid,
        "tag": tag,
        "count": len(rows),
        "kinds": kinds,
        "has_silent": kinds.get("silent", 0) > 0,
        "has_audio": kinds.get("audio", 0) > 0,
        "has_subs": kinds.get("subs", 0) > 0,
        "has_notes": kinds.get("docs", 0) > 0,
        "master_ids": master_ids,
        "clip_ids": clip_ids,
        "component_ids": component_ids,
        "clips_missing_parent": clips_missing_parent,
        "asr_ids": asr_ids,
        "missing_thumbs": missing_thumbs,
        "missing_shots": missing_shots,
        "broken_ids": broken_ids,
        "materials": [
            {"id": m["id"], "kind": m.get("kind"), "name": m.get("name", "")}
            for m in rows[:40]
        ],
    }


# ───────────── Agentic DAM 补齐能力:治理 / 分发就绪 / 持续学习 ─────────────
# 对应网上 Agentic DAM 最佳实践的 Governance / Distribution / Learning 三类缺口。

_PLACEHOLDER_RE = re.compile(
    r"(?i)(temp|tmp|草稿|占位|placeholder|draft|未命名|untitled|新建|copy|副本|\.bak|test_|_test)"
)


def governance_report(limit=50):
    """全库治理/合规只读扫描(Agent「治理合规」技能用)。

    检查:占位/临时文件、缺描述、未分类(无 role:/type:)、无语义标签且无机标。
    返回每类问题与样本 id(各限 limit)。不含任何写操作。
    """
    issues = {"placeholder": [], "missing_desc": [],
              "missing_role": [], "untagged": []}
    scanned = 0
    for m in all_materials():
        scanned += 1
        mid = m["id"]
        name = (m.get("name") or "")
        tags = _material_tag_set(m)
        desc = (m.get("description") or "").strip()
        ai_tags = (m.get("ai_tags") or "").strip()
        if _PLACEHOLDER_RE.search(name):
            issues["placeholder"].append(mid)
        if not desc:
            issues["missing_desc"].append(mid)
        k = m.get("kind") or ""
        if k in VIDEO_LIKE_KINDS and not (
            any(t.startswith("role:") for t in tags)
            or any(t.startswith("type:") for t in tags)
        ):
            issues["missing_role"].append(mid)
        semantic = [t for t in tags
                    if not (t.startswith("job:") or t.startswith("parent:")
                            or t == k or t.startswith("kind:"))]
        if not semantic and not ai_tags:
            issues["untagged"].append(mid)
    for key in issues:
        issues[key] = issues[key][:limit]
    return {
        "scanned": scanned,
        "issues": issues,
        "counts": {k: len(v) for k, v in issues.items()},
    }


def distribution_readiness(job_id=""):
    """某 job 的分发渠道就绪度评估(只读,复用 job_checkup)。

    判定 channel_ready:体检 ok 且 封面/镜头/clip 父链/标签 齐备。
    返回 blocking 清单(阻碍分发的项)与 checkup 摘要。
    """
    c = job_checkup(job_id)
    if not c.get("ok"):
        return {"job": job_id, "channel_ready": False,
                "reason": c.get("error") or "job_checkup not ok",
                "blocking": ["job_checkup_failed"], "checkup": c}
    blocking = []
    if c.get("missing_thumbs"):
        blocking.append("missing_thumbs:" + ",".join(c["missing_thumbs"][:5]))
    if c.get("missing_shots"):
        blocking.append("missing_shots:" + ",".join(c["missing_shots"][:5]))
    if c.get("clips_missing_parent"):
        blocking.append("clips_missing_parent:" + ",".join(c["clips_missing_parent"][:5]))
    if not c.get("master_ids"):
        blocking.append("no_master")
    return {
        "job": job_id,
        "channel_ready": not blocking,
        "blocking": blocking,
        "masters": len(c.get("master_ids", [])),
        "checkup_summary": {
            "kinds": c.get("kinds"),
            "missing_thumbs": len(c.get("missing_thumbs", [])),
            "missing_shots": len(c.get("missing_shots", [])),
            "clips_missing_parent": len(c.get("clips_missing_parent", [])),
        },
    }


_FEEDBACK_PATH = None


def _feedback_path():
    global _FEEDBACK_PATH
    if _FEEDBACK_PATH is None:
        _FEEDBACK_PATH = os.path.join(HUB, ".agent_feedback.jsonl")
    return _FEEDBACK_PATH


def log_feedback(action, accepted, note="", by="agent"):
    """记录一次 Agent 动作被采纳/否决(持续学习闭环,仅追加写)。

    action: 动作名(如 job_checkup/governance_report/update_tags);
    accepted: True=采纳,False=否决(override)。返回累计条数。
    """
    action = (action or "").strip()
    if not action:
        return {"ok": False, "error": "action required"}
    rec = {"ts": datetime.datetime.now().isoformat(),
           "action": action, "accepted": bool(accepted),
           "note": str(note or ""), "by": str(by or "agent")}
    try:
        with open(_feedback_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    n = 0
    try:
        with open(_feedback_path(), "r", encoding="utf-8") as f:
            n = sum(1 for _ in f)
    except OSError:
        n = 0
    return {"ok": True, "action": action, "accepted": bool(accepted), "count": n}


def learning_summary(limit=50):
    """汇总 Agent 反馈学习日志(只读):各动作采纳率 + 近期记录。"""
    path = _feedback_path()
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return {"total": 0, "by_action": {}, "recent": []}
    by_action = {}
    for r in rows:
        a = r.get("action", "?")
        d = by_action.setdefault(a, {"accepted": 0, "overrides": 0, "total": 0})
        d["total"] += 1
        if r.get("accepted"):
            d["accepted"] += 1
        else:
            d["overrides"] += 1
    for a, d in by_action.items():
        d["rate"] = round(d["accepted"] / d["total"], 3) if d["total"] else 0.0
    recent = rows[-limit:] if limit else rows
    return {"total": len(rows), "by_action": by_action, "recent": recent}


def split_new_videos(mids, *, force=False):
    """对一批素材 id 里 kind=videos 且有音轨的条目跑 split_video_to_silent_and_audio。
    跳过 role:clip(除非 force)。返回 {ran, ok, results, new_ids}。"""
    results, new_ids = [], []
    for mid in mids or []:
        m = get_material(mid)
        if not m or m.get("kind") != "videos":
            continue
        if is_role_clip(m) and not force:
            results.append({"status": "skipped", "reason": "role:clip", "source_id": mid})
            continue
        r = split_video_to_silent_and_audio(mid, force=force)
        results.append(r)
        if r.get("silent_status") == "added" and r.get("silent_id"):
            new_ids.append(r["silent_id"])
        if r.get("audio_status") == "added" and r.get("audio_id"):
            new_ids.append(r["audio_id"])
    return {
        "ran": len(results),
        "ok": sum(1 for r in results if r.get("status") in ("ok", "reclassified")),
        "results": results,
        "new_ids": new_ids,
    }


if __name__ == "__main__":
    init_hub()
    print("hub initialized at", HUB)
