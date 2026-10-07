# -*- coding: utf-8 -*-
"""素材命名规范(canonical naming)—— 显示名自解释、可区分、可溯源。

最佳实践依据(2026-10-08 调研):
- **UW Records Management**:好的命名让人"不打开文件内容就能识别它";
  强命名 = 按职能分组 + 易于识别单条记录 + 不过度复杂。
- **Canto Top 10 DAM**:#2 按业务组织、#3 受控分类法 —— 名称是最常被检索的元数据。
- **Adobe AEM 元数据**:摄入即应用元数据;名称/标题应自解释。
- **Cloudinary MAM 2026**:AI 检索成败的最大因素是**一致的元数据**;命名须统一。

提炼的六条硬规则:
① **描述性**——一眼知道是什么(阶段/内容/类型/语种)
② **一致性**——统一分隔符 `_`、统一小写英文 + 中文描述、统一"泛→具体"顺序
③ **无空格与特殊字符**(跨平台/URL 安全)
④ **唯一性**——重名即不可分辨,必须消歧
⑤ **保留原始标识以溯源**(原名 stem 留在名字里)
⑥ **只改显示名,绝不改磁盘文件**

本项目规范(索引显示名 `name`,顺序「泛 → 具体」):
    {中文描述}_{项目短码}_{原名stem}.{ext}
- 中文描述:由 token→中文 映射表 + `type:` / `lang:` 受控标签推导(自解释、利于中文检索)
- 项目短码:`job:`(无则 `parent:`,再无则素材 id)前 6 位 —— 消歧 + 按项目/源片归组
- 原名stem:保留英文 token —— ① 溯源(对应磁盘真实文件)② 保住英文/跨语查询的精确命中
- 仍重名则追加素材 id 片段(`unique_name`),保证全局唯一

铁律:真实文件名始终保存在 `orig_name`(注册时写入);`rel_path` / `external_path`
一律不动,上游 external 引用的磁盘文件绝不改写(破坏引用 = 破坏上游管线)。
"""
import os
import re

# ---------- token → 中文描述(顺序敏感:先匹配更具体的) ----------
# 依据:本库 384 条语料的实际命名 token(2026-10-08 全量扫描归纳)
_STAGE_RULES = [
    # --- 多词/整名优先(避免被单词规则抢先) ---
    ("media_status", "媒体状态"),
    ("pack_manifest", "打包清单"),
    ("job_summary", "作业摘要"),
    ("sync_meta", "同步元数据"),
    ("clips_meta", "片段元数据"),
    ("danmaku_hotwords", "弹幕热词"),
    ("clean_concat", "拼接清单"),
    ("source_video", "源片音频"),
    ("hot_clean", "热区降噪"),
    ("hot_src", "热区源帧"),
    ("flat_probe", "平场探测"),
    ("glyph_black", "字形黑底"),
    ("glyph_tbe", "字形渲染"),
    ("old_sttn", "原片去字幕"),
    ("narrow_box", "窄区"),
    ("out_sttn_combined", "去字幕合并成品"),
    ("hs_r", "去硬字幕"),
    ("general", "通用"),          # 必须早于 "gen",否则 general_meta 会被误判为「生成」
    # --- 音频优先(否则会被 clean/source 抢先) ---
    ("track", "音轨"),
    ("16k", "16k音频"),
    # --- 处理阶段 ---
    ("dehardsub", "去硬字幕"),
    ("hardsub", "硬字幕"),
    ("delogo", "去台标"),
    ("demosaic", "去马赛克"),
    ("mosaic", "马赛克"),
    ("deblur", "去模糊"),
    ("upscal", "超分"),
    ("codeformer", "人脸修复"),
    ("sttn", "去字幕"),
    ("lama", "修补"),
    ("glyph", "字形修复"),
    ("tbe", "字形渲染"),
    ("blackfill", "黑色填充"),
    ("fillcrop", "填充裁剪"),
    ("srccrop", "源区裁剪"),
    ("crop", "裁剪"),
    ("fill", "填充"),
    ("fullwidth", "全幅"),
    ("probe", "探测"),
    ("clean", "降噪"),
    ("fixed", "修复"),
    ("combined", "合并"),
    ("range", "片段"),
    ("keep", "保留段"),
    ("clips", "片段清单"),
    ("clip", "片段"),
    ("frame", "关键帧"),
    ("final", "成品"),
    ("poster", "封面"),
    ("found", "检出区"),
    ("roi", "区域"),
    ("band", "字幕带"),
    ("gen2", "生成v2"),
    ("gen", "生成"),
    ("gpu_batch", "批量GPU"),
    ("hybrid", "混合"),
    ("writing", "文字区"),
    ("concat", "拼接"),
    ("multipass", "多轮"),
    ("sync", "同步"),
    ("summary", "摘要"),
    ("metrics", "指标"),
    ("manifest", "打包清单"),
    ("pack", "打包"),
    ("status", "状态"),
    ("progress", "进度"),
    ("meta", "元数据"),
    ("glossary", "术语表"),
    ("translate", "翻译"),
    ("fetch", "抓取"),
    ("old", "原片"),
    ("src", "源帧"),
    ("source", "源片"),
    ("in_", "输入片段"),
    ("out_", "输出片段"),
    ("demux", "分离"),
]

# ---------- 语种(受控 lang: 标签)→ 中文 ----------
# 注:这里**没有** kind/type 的泛化类目表——那些词(视频/图/文档/音频/成品/片段)
# 刻意不进显示名,原因见 canonical_name 内的注释(泛化词在 name 高权重下会扰乱排序)。
_LANG_ZH = {
    "zh": "中文", "zh-hans": "简体中文", "zh-hant": "繁体中文", "zh-hk": "繁体中文",
    "en": "英文", "ja": "日文", "ko": "韩文", "ru": "俄文", "fr": "法文",
    "de": "德文", "es": "西班牙文", "pt": "葡萄牙文", "ar": "阿拉伯文",
    "hi": "印地文", "id": "印尼文", "th": "泰文", "tr": "土耳其文", "vi": "越南文",
}


def _tag_val(tags, key):
    """从 tags 串里取 `key:value` 的 value(受控标签)。"""
    for t in (tags or "").split(","):
        t = t.strip()
        if t.startswith(key + ":"):
            return t.split(":", 1)[1].strip()
    return ""


def _norm_stem(stem):
    """规范化(用于项目短码这类分组标识):去掉首尾下划线、连字符统一为下划线。"""
    s = re.sub(r"[^0-9A-Za-z_.\-]", "_", stem or "")
    s = s.replace("-", "_")
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "asset"


def _safe_stem(stem):
    """原名词干:**只**替换文件名不安全字符,其余一律原样保留。

    刻意不做激进规范化(不改 `-`、不去前导下划线之外的字符):规范名必须**逐字符包含**
    真实文件名,否则会丢掉 token —— 实测 40 条素材因规范化后显示名不再包含完整
    真实文件名,导致 r20 0.78→0.76(GT 相关项被挤出前 20)。溯源与检索都依赖这个保真。
    """
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", stem or "")
    return s.strip("_") or "asset"


def canonical_name(name, kind="", tags="", mid="", description=""):
    """生成规范显示名 `{中文描述}_{原名stem}_{项目短码}.{ext}`。

    纯函数:只依赖入参,不改任何磁盘/索引。原名 token 完整保留在名字里,
    故既保住英文/跨语检索的精确命中,又保留溯源能力。
    """
    stem, ext = os.path.splitext(name or "")
    low = (stem or "").lower()
    stem_n = _safe_stem(stem)      # 保真:逐字符保留真实文件名(溯源 + 不丢 token)

    # 1) 中文描述:只用**具体**的处理阶段词。刻意**不做** kind/type 兜底——
    #    "视频/图/文档/音频/无声视频" 这类泛化词写在 name(权重 3.0,全场最高)里,
    #    会与通用查询词大面积碰撞:实测「基准测试视频」被 hub_up_test(基准测试)与
    #    AttackOnTitan(视频)挤掉 GT 前排,P@5 1.00→0.00、总 r20 0.78→0.76。
    #    最佳实践要的是"描述性"= **具体**,不是泛化类目词。
    zh = ""
    for pat, z in _STAGE_RULES:
        if pat in low:
            zh = z
            break
    if not zh and kind == "subs":
        zh = "字幕"                  # 字幕文件名往往只是语种码,补一个具体类目
    parts = [zh] if zh else []

    # 2) 语种(字幕类最关键;其他类有 lang 标签也补上)
    lang = _tag_val(tags, "lang")
    if not lang and kind == "subs":
        lang = stem_n          # 字幕文件名本身往往就是语种码(zh/en/ja…)
    if lang:
        # stem 已把 "-" 归一成 "_",故查表时要还原成 "-" 才能命中 zh-hant 这类键
        key = lang.lower().replace("_", "-")
        parts.append(_LANG_ZH.get(key) or _LANG_ZH.get(lang.lower()) or lang)

    # 3) 项目短码(消歧):job: → parent:(demux 子素材归到源片) → 素材 id
    job = _tag_val(tags, "job") or _tag_val(tags, "parent") or (mid or "")
    parts.append(_norm_stem(job)[:6] or "nogroup")

    return "%s_%s%s" % ("_".join(parts), stem_n, ext or "")


def unique_name(name, mid=""):
    """二次消歧:同名时追加素材 id 片段,保证全局唯一。"""
    return "%s_%s%s" % (os.path.splitext(name)[0], (mid or "")[:4], os.path.splitext(name)[1])


def plan_renames(materials):
    """批量生成改名方案,返回 [(material, new_name)]。纯计算,不写库。

    **幂等性**:规范名一律基于 `orig_name`(磁盘真实文件名)推导,而非当前显示名——
    否则重复规范化会不断叠加中文前缀(`去台标_x_去台标_x_001_delogo.mp4`)。
    """
    out, seen = [], {}
    for m in materials:
        mid = m.get("id", "")
        src = (m.get("orig_name") or "").strip() or (m.get("name") or "")
        new = canonical_name(src, m.get("kind", ""), m.get("tags", ""),
                             mid=mid, description=m.get("description", ""))
        if new in seen:                       # 仍重名 → 追加 id 片段
            new = unique_name(new, mid)
        seen[new] = mid
        out.append((m, new))
    return out
