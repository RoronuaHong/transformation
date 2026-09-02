import type { Locale } from "./locales";

export type FeedCopy = {
  kicker: string;
  headline: string;
  lede: string;
  cta: string;
  empty: string;
  searchPlaceholder: string;
  colTitle: string;
  colPlatform: string;
  colTopic: string;
  colHighlights: string;
  colPoints: string;
  colAction: string;
  pagePrev: string;
  pageNext: string;
  pageStatus: string;
  perPage: string;
  noResults: string;
};

export type ArticleCopy = {
  notes: string;
  overview: string;
  openNotes: string;
  closeNotes: string;
  focuses: string;
  keyPoints: string;
  hardPoints: string;
  source: string;
  feed: string;
  captions: string;
    downloadCaptions: string;
    downloadNotes: string;
    downloadClips: string;
  downloadClipItem: string;
  downloadClipFile: string;
  previewClip: string;
  viewFrame: string;
  mediaPreview: string;
  mediaGifKind: string;
  mediaTabClip: string;
  clipsEmpty: string;
  segmentCaptions: string;
  downloadSegmentCaptions: string;
  disclaimer: string;
  watchYoutube: string;
  watchBilibili: string;
  watchDouyin: string;
  unlock: string;
  unlocking: string;
  lockedHint: string;
  morePoints: string;
  remixPlay: string;
};

export type NavCopy = {
  language: string;
  brand: string;
  feed: string;
  ask: string;
  themeLight: string;
  themeDark: string;
  themeToLight: string;
  themeToDark: string;
};

export type TryCopy = {
  kicker: string;
  headline: string;
  lede: string;
  tabUrl: string;
  tabUpload: string;
  urlLabel: string;
  urlPlaceholder: string;
  urlHint: string;
  urlsParsed: string;
  urlsInvalid: string;
  sessionidLabel: string;
  sessionidPlaceholder: string;
  cookiesOk: string;
  cookiesNeed: string;
  cookiesHint: string;
  cookiesPlatformTitle: string;
  cookiesPlatformRequired: string;
  cookiesPlatformOptional: string;
  cookiesPlatformSoft: string;
  cookiesPlatformReady: string;
  cookiesBlockBatch: string;
  cookiesLocalDetected: string;
  batchQueued: string;
  batchJobsTitle: string;
  batchSummary: string;
  progressNow: string;
  jobCancel: string;
  jobRetry: string;
  queuePause: string;
  queueResume: string;
  queuePaused: string;
  modulesSection: string;
  moduleTranslate: string;
  moduleNotes: string;
  moduleClips: string;
  modulesHint: string;
  sourceSection: string;
  langsSection: string;
  langsHint: string;
  langsSelectAll: string;
  langsClear: string;
  langsNeedOne: string;
  langsPick: string;
  langsDone: string;
  langsAllLabel: string;
  intentFull: string;
  intentPost: string;
  intentClips: string;
  intentMedia: string;
  intentPostproc: string;
  intentNoop: string;
  intentCached: string;
  mediaSection: string;
  fileLabel: string;
  submit: string;
  reset: string;
  submitting: string;
  processing: string;
  busy: string;
  queueAhead: string;
  queuePosition: string;
  statusPending: string;
  statusProcessing: string;
  statusDone: string;
  statusFailed: string;
  statusCancelled: string;
  openNotes: string;
  hint: string;
  apiDown: string;
  progressActive: string;
  progressStale: string;
  stageQueued: string;
  stageDownload: string;
  stageTranscribe: string;
  stageTranslate: string;
  stageNotes: string;
  stageLocalize: string;
  stageGif: string;
  stageExport: string;
  stageDone: string;
  stageFailed: string;
  stageCancelled: string;
  progressLangs: string;
  alreadyDone: string;
  framesLabel: string;
  framesAuto: string;
  framesNone: string;
  framesGif: string;
  framesJpg: string;
  gifRangesLabel: string;
  gifSecLabel: string;
  videoDurationHint: string;
  framesHint: string;
  framesBlockTitle: string;
  gifAdd: string;
  gifTooLong: string;
  clipsBlockTitle: string;
  clipsLabel: string;
  clipsStart: string;
  clipsEnd: string;
  clipsAdd: string;
  clipsRemove: string;
  clipsHint: string;
  clipsNeedBoth: string;
  clipsEndAfterStart: string;
  clipsOverDuration: string;
  clipsInvalidNumber: string;
  clipsTooLong: string;
  linkMediaTitle: string;
  linkMediaOverride: string;
  linkMediaExpand: string;
  linkMediaCollapse: string;
  linkMediaUseGlobal: string;
  filesHint: string;
  filesParsed: string;
  filesNeedOne: string;
  filesDropTitle: string;
  filesDropBrowse: string;
  filesDropTypes: string;
  filesClear: string;
  defaultsTitle: string;
  applyDefaultsAll: string;
  workflowMediaHint: string;
  workflowMediaEmpty: string;
  workflowRailSource: string;
  workflowRailTranslate: string;
  workflowRailNotes: string;
  workflowRailClips: string;
  workflowRailEnhance: string;
  workflowRailCompress: string;
  workflowRailConcat: string;
  workflowRailRemix: string;
  workflowRailPublish: string;
  workflowRailLangs: string;
  workflowRailSoon: string;
  workflowRailHint: string;
  enhanceSection: string;
  enhanceHint: string;
  enhanceStrength: string;
  enhanceLight: string;
  enhanceMedium: string;
  enhanceStrong: string;
  compressSection: string;
  compressHint: string;
  compressHeight: string;
  compressKeep: string;
  compressCrf: string;
  concatHint: string;
  concatNeedTwo: string;
  remixSection: string;
  remixHint: string;
  derivedTitle: string;
  derivedEnhance: string;
  derivedCompress: string;
  derivedConcat: string;
  derivedRemix: string;
  derivedRemixVtt: string;
  stageEnhance: string;
  stageCompress: string;
  stageConcat: string;
  stageRemix: string;
  stagePublish: string;
  clipsSection: string;
  accountsSection: string;
  accountsHint: string;
  accountBind: string;
  accountUnbind: string;
  accountValid: string;
  accountInvalid: string;
  accountUnbound: string;
  accountSecret: string;
  accountLabel: string;
  accountId: string;
  accountDouyin: string;
  accountKuaishou: string;
  accountNeedBind: string;
  publishSection: string;
  publishHint: string;
  derivedPublish: string;
};

export type AskCopy = {
  lede: string;
  placeholder: string;
};

export type UiCopy = {
  feed: FeedCopy;
  article: ArticleCopy;
  nav: NavCopy;
  try: TryCopy;
  ask: AskCopy;
};

const en: UiCopy = {
  feed: {
    kicker: "Library",
    headline: "Published video notes",
    lede: "Browse videos that have been transcribed, translated, and exported.",
    cta: "Open notes",
    empty: "No published notes yet. Process a video on the home page.",
    searchPlaceholder: "Search title, topic, or key points…",
    colTitle: "Title",
    colPlatform: "Platform",
    colTopic: "Topic",
    colHighlights: "Highlights",
    colPoints: "Points",
    colAction: "Action",
    pagePrev: "Previous",
    pageNext: "Next",
    pageStatus: "{from}–{to} of {total}",
    perPage: "Per page",
    noResults: "No notes match your search.",
  },
  article: {
    notes: "Notes",
    overview: "Overview",
    openNotes: "Open notes",
    closeNotes: "Close",
    focuses: "Core points",
    keyPoints: "Key points",
    hardPoints: "Watch-outs",
    source: "Source",
    feed: "Library",
    captions: "Line-by-line captions",
    downloadCaptions: "Download captions (.srt)",
    downloadNotes: "Download notes",
    downloadClips: "Download MP4 clips",
    downloadClipItem: "Clip",
    downloadClipFile: "Download",
    previewClip: "Preview",
    viewFrame: "Click to view",
    mediaPreview: "Media",
    mediaOpen: "View media",
    mediaGifKind: "GIF",
    mediaTabClip: "MP4",
    mediaBack: "Back to list",
    mediaCount: "{n} items",
    clipsEmpty:
      "No MP4 clips yet. On the home page, fill Block 2 start/end seconds (e.g. 10→20), then generate. Block 1 GIF length does not create downloads here.",
    segmentCaptions: "Captions in this clip",
    downloadSegmentCaptions: "Download clip captions (.srt)",
    disclaimer:
      "This site publishes written notes and short step clips only. We do not host the full original video. Watch the full demo on the source platform.",
    watchYoutube: "Watch on YouTube",
    watchBilibili: "Watch on Bilibili",
    watchDouyin: "Watch on Douyin",
    unlock: "Watch a short ad to unlock full notes",
    unlocking: "Ad playing",
    lockedHint: "Core points and watch-outs unlock after the ad.",
    morePoints: "{n} more points",
    remixPlay: "9:16 remix (captions overlay, not burned in)",
  },
  nav: {
    language: "Language",
    brand: "Vitual",
    feed: "Library",
    ask: "Ask",
    themeLight: "Light",
    themeDark: "Dark",
    themeToLight: "Switch to light",
    themeToDark: "Switch to dark",
  },
  try: {
    kicker: "Batch workflow",
    headline: "Add links or files, then set clips per item",
    lede: "Paste multiple links or upload several videos. Each item gets its own GIF/MP4 ranges, then multilingual notes. Bilibili, YouTube, Douyin, or local mp4/mov — usually 30–90 minutes per item.",
    tabUrl: "Paste links",
    tabUpload: "Upload files",
    urlLabel: "Video links",
    urlPlaceholder: "One link per line\nBilibili / YouTube / Douyin",
    urlHint: "One link per line. Duplicates are removed automatically.",
    urlsParsed: "Found {n} links",
    urlsInvalid: "{n} lines could not be parsed",
    sessionidLabel: "Login cookies (optional)",
    sessionidPlaceholder: "Paste Netscape cookies.txt (best for batches), or sessionid=… for a single platform",
    cookiesOk: "Login cookies look usable.",
    cookiesNeed: "Login cookies required — paste above, then submit.",
    cookiesHint: "Saved locally as cookies.txt for downloads only. One paste applies to the whole batch.",
    cookiesPlatformTitle: "By platform",
    cookiesPlatformRequired: "{platform} · {n} links · login required · missing",
    cookiesPlatformOptional: "{platform} · {n} links · no login needed",
    cookiesPlatformSoft: "{platform} · {n} links · usually no login · add cookies if download fails",
    cookiesPlatformReady: "{platform} · {n} links · ready",
    cookiesBlockBatch: "This batch needs login cookies for: {platforms}",
    cookiesLocalDetected: "Local cookies.txt detected on this machine.",
    batchQueued: "{n} items queued — processing one at a time.",
    batchJobsTitle: "Batch jobs",
    batchSummary: "{total} jobs · {running} running · {queued} queued",
    progressNow: "Now: {stage}",
    jobCancel: "Cancel",
    jobRetry: "Retry",
    queuePause: "Pause queue",
    queueResume: "Resume queue",
    queuePaused: "Queue paused — current job finishes, then no new jobs start.",
    modulesSection: "Workflows",
    moduleTranslate: "Subtitles",
    moduleNotes: "Notes",
    moduleClips: "Clips",
    modulesHint: "Toggle which workflows run on this batch.",
    sourceSection: "Sources",
    langsSection: "Languages",
    clipsSection: "Clips per item",
    langsHint: "Pick target languages for captions and notes. The spoken source language is always kept even if unchecked here.",
    langsSelectAll: "Select all",
    langsClear: "Clear",
    langsNeedOne: "Select at least one language.",
    langsPick: "Choose languages",
    langsDone: "Done",
    langsAllLabel: "All languages",
    intentFull: "This run: full pipeline (download → notes).",
    intentPost: "This run: re-translate {n} languages (reuse transcription).",
    intentClips: "This run: re-cut MP4 only (no ASR).",
    intentMedia: "This run: refresh GIF / MP4 only (no ASR).",
    intentPostproc: "This run: enhance / compress / concat / remix / publish only (no ASR).",
    intentNoop: "Already done — nothing to change. Open notes below.",
    intentCached: "Notes already exist for this link.",
    mediaSection: "Clips per item",
    fileLabel: "Video files",
    submit: "Start workflow",
    reset: "Reset",
    submitting: "Queued…",
    processing: "Processing…",
    busy: "Only one try job runs at a time. Yours is queued — please wait.",
    queueAhead: "Running now: {title} · {stage} {percent}%",
    queuePosition: "Your job is #{n} in line — starts automatically when the current task finishes.",
    statusPending: "Queued",
    statusProcessing: "Processing",
    statusDone: "Done",
    statusFailed: "Failed",
    statusCancelled: "Cancelled",
    openNotes: "Open notes",
    hint: "We publish written notes and short clips only — not the full original video.",
    apiDown: "Backend offline. Start subtitle_pipeline with yarn api on port 8901.",
    progressActive: "Running",
    progressStale: "No updates for 5+ minutes — may be stuck",
    stageQueued: "Waiting to start",
    stageDownload: "Downloading audio/video",
    stageTranscribe: "Transcribing speech",
    stageTranslate: "Translating subtitles",
    stageNotes: "Writing notes",
    stageLocalize: "Localizing notes",
    stageGif: "Extracting step GIFs",
    stageExport: "Publishing to site",
    stageDone: "Complete",
    stageFailed: "Failed",
    stageCancelled: "Cancelled",
    progressLangs: "{n}/{total} languages",
    alreadyDone: "Notes already exist for this video.",
    framesBlockTitle: "GIF / stills",
    framesLabel: "Step images (GIF / still)",
    framesAuto: "Auto (recommend)",
    framesNone: "No images",
    framesGif: "GIF only",
    framesJpg: "Still only",
    gifRangesLabel: "GIF / still ranges (start → end)",
    gifSecLabel: "GIF length in seconds (1–20)",
    videoDurationHint: "Video length ≈ {n}s — start/end cannot exceed this.",
    framesHint:
      "Same as MP4: fill start/end seconds, add more rows if needed. Each row cuts one GIF/still. Leave empty with Auto to follow subtitle steps (default 4s).",
    gifAdd: "Add GIF range",
    gifTooLong: "Each GIF/still range can be at most {n}s long.",
    clipsBlockTitle: "MP4 clips",
    clipsLabel: "MP4 ranges (start → end)",
    clipsStart: "Start time (s)",
    clipsEnd: "End time (s)",
    clipsAdd: "Add range",
    clipsRemove: "Remove",
    clipsHint:
      "Cuts short MP4 from start→end. Finished job + unchanged languages + “No images” → re-cut MP4 only. Change the language list to re-translate.",
    clipsNeedBoth: "Fill both start and end for each range.",
    clipsEndAfterStart: "End must be greater than start.",
    clipsOverDuration: "Start/end cannot exceed video length ({n}s).",
    clipsInvalidNumber: "Enter a valid time in seconds.",
    clipsTooLong: "Each MP4 range can be at most {n}s long.",
    linkMediaTitle: "Items in this batch",
    linkMediaOverride: "Custom GIF/MP4 for this item",
    linkMediaExpand: "Edit clips",
    linkMediaCollapse: "Hide",
    linkMediaUseGlobal: "Uses defaults above",
    filesHint: "Each file gets its own GIF/MP4 card in step 2.",
    filesParsed: "{n} files selected",
    filesNeedOne: "Select at least one file.",
    filesDropTitle: "Drop videos here",
    filesDropBrowse: "or browse files",
    filesDropTypes: "mp4 · mov · mkv · webm · m4a · wav",
    filesClear: "Clear all",
    defaultsTitle: "Shared defaults",
    applyDefaultsAll: "Sync first item → all",
    workflowMediaHint: "Each item has its own GIF / MP4 ranges.",
    workflowMediaEmpty: "Add a link or file above — a clip card appears for each item.",
    workflowRailSource: "Source",
    workflowRailTranslate: "Translate",
    workflowRailNotes: "Notes",
    workflowRailClips: "Clips",
    workflowRailEnhance: "Enhance",
    workflowRailCompress: "Compress",
    workflowRailConcat: "Concat",
    workflowRailRemix: "Remix",
    workflowRailPublish: "Publish",
    workflowRailLangs: "Languages",
    workflowRailSoon: "Soon",
    workflowRailHint: "Tap to enable workflows for this run.",
    enhanceSection: "Enhance",
    enhanceHint: "Sharpen the working video (concat if you made one, otherwise the source). No AI upscaler in this version.",
    enhanceStrength: "Strength",
    enhanceLight: "Light",
    enhanceMedium: "Medium",
    enhanceStrong: "Strong (1.5× then cap)",
    compressSection: "Compress",
    compressHint: "Re-encode with H.264 for a smaller file. Runs after concat/enhance, before remix.",
    compressHeight: "Max height",
    compressKeep: "Keep size",
    compressCrf: "Quality (CRF, lower = larger)",
    concatHint: "Joins this item’s MP4 ranges in order. Need at least two ranges.",
    concatNeedTwo: "Concat needs at least two MP4 ranges on each item.",
    remixSection: "Remix",
    remixHint:
      "Makes a 9:16 vertical cut (1080×1920): notes one-liner as an overlay title, then the working clip. Captions follow the player clock — they are not burned into the file. No TTS.",
    derivedTitle: "Processed video",
    derivedEnhance: "Download enhanced MP4",
    derivedCompress: "Download compressed MP4",
    derivedConcat: "Download concatenated MP4",
    derivedRemix: "Download 9:16 remix MP4",
    derivedRemixVtt: "Download overlay captions (.vtt)",
    stageEnhance: "Enhancing video",
    stageCompress: "Compressing video",
    stageConcat: "Concatenating clips",
    stageRemix: "Making 9:16 remix",
    stagePublish: "Publishing",
    accountsSection: "Publish accounts",
    accountsHint:
      "Separate from download cookies. Stored only on this machine; never exported to the site.",
    accountBind: "Bind",
    accountUnbind: "Unbind",
    accountValid: "Valid",
    accountInvalid: "Invalid",
    accountUnbound: "Not bound",
    accountSecret: "Creator cookies / session",
    accountLabel: "Label",
    accountId: "Account id",
    accountDouyin: "Douyin",
    accountKuaishou: "Kuaishou",
    accountNeedBind: "Bind at least one valid Douyin or Kuaishou account before publishing.",
    publishSection: "Publish",
    publishHint:
      "Writes a per-platform ledger from the remix (or working video). v1 stages files locally — Douyin/Kuaishou have no official third-party upload API.",
    derivedPublish: "Download publish report",
  },
  ask: {
    lede: "Ask questions about the project knowledge base. Answers are grounded in indexed docs with sources cited.",
    placeholder: "Ask anything, e.g. Which workspace packs are available and how much do they cost?",
  },
};

const zh: UiCopy = {
  feed: {
    kicker: "已发布",
    headline: "视频笔记列表",
    lede: "浏览已完成转写、翻译并导出的视频笔记。",
    cta: "查看笔记",
    empty: "还没有已发布的笔记。请先在首页处理一条视频。",
    searchPlaceholder: "搜索标题、主题或要点…",
    colTitle: "标题",
    colPlatform: "平台",
    colTopic: "主题",
    colHighlights: "要点预览",
    colPoints: "条数",
    colAction: "操作",
    pagePrev: "上一页",
    pageNext: "下一页",
    pageStatus: "第 {from}–{to} 条，共 {total} 条",
    perPage: "每页",
    noResults: "没有匹配的笔记。",
  },
  article: {
    notes: "总结",
    overview: "总体总结",
    openNotes: "查看笔记",
    closeNotes: "关闭",
    focuses: "重点",
    keyPoints: "要点",
    hardPoints: "难点",
    source: "来源",
    feed: "列表",
    captions: "逐句字幕",
    downloadCaptions: "下载字幕（.srt）",
    downloadNotes: "下载笔记",
    downloadClips: "下载 MP4 片段",
    downloadClipItem: "片段",
    downloadClipFile: "下载",
    previewClip: "预览",
    viewFrame: "点击查看",
    mediaPreview: "画面",
    mediaOpen: "查看画面",
    mediaGifKind: "动图",
    mediaTabClip: "MP4",
    mediaBack: "返回列表",
    mediaCount: "{n} 项",
    clipsEmpty:
      "还没有 MP4 片段。请在首页「② MP4 片段」填写开始/结束秒（例如 10→20），再点生成。上方「① GIF 时长」不会出现在这里。",
    segmentCaptions: "该片段对应字幕",
    downloadSegmentCaptions: "下载片段字幕（.srt）",
    disclaimer: "本站只整理文字步骤与短片段，不托管完整原视频。完整演示请到原平台。",
    watchYoutube: "去 YouTube 观看",
    watchBilibili: "去 B 站观看",
    watchDouyin: "去抖音观看",
    unlock: "看一段广告，解锁完整笔记",
    unlocking: "广告播放中",
    lockedHint: "重点和难点在广告后开放。",
    morePoints: "还有 {n} 条",
    remixPlay: "9:16 二创（字幕叠在画面上，不烧进视频）",
  },
  nav: {
    language: "语言",
    brand: "Vitual",
    feed: "列表",
    ask: "问问一桌",
    themeLight: "浅色",
    themeDark: "深色",
    themeToLight: "切换到浅色",
    themeToDark: "切换到深色",
  },
  try: {
    kicker: "批量工作流",
    headline: "添加链接或文件，再逐条设置裁剪",
    lede: "可一次粘贴多条链接，或上传多个视频。每条各自配置 GIF/MP4 区间，再生成多语言笔记与字幕。支持 B 站、YouTube、抖音及本机 mp4/mov。",
    tabUrl: "粘贴链接",
    tabUpload: "上传文件",
    urlLabel: "视频链接",
    urlPlaceholder: "一行一个链接\nB 站 / YouTube / 抖音",
    urlHint: "一行一条链接，重复会自动去重。",
    urlsParsed: "已识别 {n} 条链接",
    urlsInvalid: "{n} 行无法解析",
    sessionidLabel: "登录态（可选）",
    sessionidPlaceholder: "批量推荐粘贴 Netscape cookies.txt 全文；或 sessionid=…（单平台）",
    cookiesOk: "登录态可用。",
    cookiesNeed: "需要登录态 — 请粘贴后提交。",
    cookiesHint: "仅写入本机 cookies.txt 用于下载，整批任务共用同一份登录态。",
    cookiesPlatformTitle: "按平台",
    cookiesPlatformRequired: "{platform} · {n} 条 · 需要登录 · 未提供",
    cookiesPlatformOptional: "{platform} · {n} 条 · 无需登录",
    cookiesPlatformSoft: "{platform} · {n} 条 · 通常无需登录 · 下载失败时可补 cookie",
    cookiesPlatformReady: "{platform} · {n} 条 · 已就绪",
    cookiesBlockBatch: "本批需要以下平台的登录态：{platforms}",
    cookiesLocalDetected: "已检测到本机 cookies.txt。",
    batchQueued: "已排队 {n} 条 — 将依次处理。",
    batchJobsTitle: "批量任务",
    batchSummary: "共 {total} 条 · {running} 进行中 · {queued} 排队",
    progressNow: "当前：{stage}",
    jobCancel: "取消",
    jobRetry: "重跑",
    queuePause: "暂停队列",
    queueResume: "继续队列",
    queuePaused: "队列已暂停 — 当前任务完成后不再启动新任务。",
    modulesSection: "本次工作流",
    moduleTranslate: "字幕翻译",
    moduleNotes: "结构化笔记",
    moduleClips: "切片",
    modulesHint: "点顶部开关选择本轮工作流；灰色为未上线。",
    sourceSection: "来源",
    langsSection: "翻译语言",
    langsHint: "勾选字幕/笔记目标语。口播源语言会自动保留，即使这里没勾也保留源字幕与源笔记。",
    langsSelectAll: "全选",
    langsClear: "清空",
    langsNeedOne: "请至少选择一种语言。",
    langsPick: "选择语言",
    langsDone: "完成",
    langsAllLabel: "全部语言",
    intentFull: "本次：全量处理（下载 → 笔记）。",
    intentPost: "本次：重翻 {n} 种语言（复用转写，不重跑 Whisper）。",
    intentClips: "本次：只重切 MP4（不跑转写）。",
    intentMedia: "本次：只刷新 GIF / MP4（不跑转写）。",
    intentPostproc: "本次：只跑增强 / 压缩 / 拼接 / 二创 / 发布（不跑转写）。",
    intentNoop: "已完成且无变更 — 可直接查看笔记。",
    intentCached: "该链接已生成过笔记。",
    mediaSection: "逐条裁剪",
    fileLabel: "视频文件",
    submit: "开始工作流",
    reset: "重置",
    submitting: "提交中…",
    processing: "处理中…",
    busy: "本机同时只处理一条任务。你的视频已排队，请稍候。",
    queueAhead: "前方正在处理：{title} · {stage} {percent}%",
    queuePosition: "你的任务排在第 {n} 位，前方任务完成后会自动开始。",
    statusPending: "排队中",
    statusProcessing: "处理中",
    statusDone: "完成",
    statusFailed: "失败",
    statusCancelled: "已取消",
    openNotes: "查看笔记",
    hint: "本站只整理文字步骤与短片段，不托管完整原视频。",
    apiDown: "后台未启动。请在 subtitle_pipeline 目录运行 yarn api（8901 端口）。",
    progressActive: "运行中",
    progressStale: "超过 5 分钟无更新，可能已卡住",
    stageQueued: "排队等待",
    stageDownload: "下载音视频",
    stageTranscribe: "语音转写",
    stageTranslate: "翻译字幕",
    stageNotes: "生成笔记",
    stageLocalize: "多语言笔记",
    stageGif: "截取步骤 GIF",
    stageExport: "导出到站点",
    stageDone: "已完成",
    stageFailed: "失败",
    stageCancelled: "已取消",
    progressLangs: "{n}/{total} 种语言",
    alreadyDone: "该视频已生成过笔记，可直接查看。",
    framesBlockTitle: "GIF / 静帧",
    framesLabel: "步骤配图（GIF / 静帧）",
    framesAuto: "自动（推荐）",
    framesNone: "不要图",
    framesGif: "只要 GIF",
    framesJpg: "只要静帧",
    gifRangesLabel: "按秒数截取 GIF / 静帧",
    gifSecLabel: "GIF 时长（秒，1–20）",
    videoDurationHint: "视频约 {n} 秒 — 开始/结束都不能超过总长。",
    framesHint:
      "与下方 MP4 一样：填开始/结束秒，可「加一段」。每行切一张 GIF/静帧。自动模式下不填则仍按字幕要点切（默认 4 秒）。",
    gifAdd: "加一段 GIF",
    gifTooLong: "单段 GIF/静帧最长 {n} 秒，请缩短区间。",
    clipsBlockTitle: "MP4 片段",
    clipsLabel: "按秒数截取 MP4",
    clipsStart: "开始时间（秒）",
    clipsEnd: "结束时间（秒）",
    clipsAdd: "加一段",
    clipsRemove: "删除",
    clipsHint:
      "按「开始→结束」切短 MP4。已完成且语言未改：选「不要图」+ 填区间 → 只重切。改语言列表 → 重翻（复用转写）。",
    clipsNeedBoth: "每一段的开始、结束都要填。",
    clipsEndAfterStart: "结束时间必须大于开始时间。",
    clipsOverDuration: "开始/结束不能超过视频总长（{n} 秒）。",
    clipsInvalidNumber: "请填写有效的秒数。",
    clipsTooLong: "单段 MP4 最长 {n} 秒，请缩短区间。",
    linkMediaTitle: "本批条目",
    linkMediaOverride: "本条使用独立 GIF/MP4 设置",
    linkMediaExpand: "编辑裁剪",
    linkMediaCollapse: "收起",
    linkMediaUseGlobal: "使用上方默认设置",
    filesHint: "每个文件会在步骤「逐条裁剪」里出现独立卡片。",
    filesParsed: "已选 {n} 个文件",
    filesNeedOne: "请至少选择一个文件。",
    filesDropTitle: "把视频拖到这里",
    filesDropBrowse: "或点击选择文件",
    filesDropTypes: "mp4 · mov · mkv · webm · m4a · wav",
    filesClear: "清空",
    defaultsTitle: "共用默认",
    applyDefaultsAll: "将第一条同步到全部",
    workflowMediaHint: "每条链接或每个文件各自配置 GIF 与 MP4。",
    workflowMediaEmpty: "先在上方添加链接或文件，每条会出现一张裁剪卡片。",
    clipsSection: "逐条裁剪",
    workflowRailSource: "来源",
    workflowRailTranslate: "翻译",
    workflowRailNotes: "笔记",
    workflowRailClips: "切片",
    workflowRailEnhance: "增强",
    workflowRailCompress: "压缩",
    workflowRailConcat: "拼接",
    workflowRailRemix: "二创",
    workflowRailPublish: "发布",
    workflowRailLangs: "语言",
    workflowRailSoon: "待上线",
    workflowRailHint: "点选本轮要跑的工作流。",
    enhanceSection: "画质增强",
    enhanceHint: "锐化当前工作视频（有拼接则用拼接结果，否则用源片）。本版不用 AI 超分。",
    enhanceStrength: "强度",
    enhanceLight: "轻",
    enhanceMedium: "中",
    enhanceStrong: "强（先放大再限高）",
    compressSection: "缩小体积",
    compressHint: "H.264 重编码压体积，接在增强或拼接之后、二创之前。",
    compressHeight: "高度上限",
    compressKeep: "保持原分辨率",
    compressCrf: "质量 CRF（越小越大）",
    concatHint: "按顺序拼接本条的 MP4 区间，至少两段。",
    concatNeedTwo: "拼接需要每条至少两段 MP4 区间。",
    remixSection: "竖屏二创",
    remixHint:
      "做成 9:16（1080×1920）成片：片头用笔记一句话叠在画面上，正片为当前工作视频。字幕跟播放时钟走，不烧进文件。不做配音。",
    derivedTitle: "处理后的视频",
    derivedEnhance: "下载增强版 MP4",
    derivedCompress: "下载压缩版 MP4",
    derivedConcat: "下载拼接 MP4",
    derivedRemix: "下载 9:16 二创 MP4",
    derivedRemixVtt: "下载叠加字幕（.vtt）",
    stageEnhance: "画质增强",
    stageCompress: "压缩体积",
    stageConcat: "拼接片段",
    stageRemix: "竖屏二创",
    stagePublish: "发布台账",
    accountsSection: "发布账号",
    accountsHint: "与下载 cookies 分开。只存在本机，不会进站点导出。",
    accountBind: "绑定",
    accountUnbind: "解绑",
    accountValid: "有效",
    accountInvalid: "失效",
    accountUnbound: "未绑定",
    accountSecret: "创作者 cookies / 登录态",
    accountLabel: "备注名",
    accountId: "账号 id",
    accountDouyin: "抖音",
    accountKuaishou: "快手",
    accountNeedBind: "发布前请先绑定至少一个有效的抖音或快手账号。",
    publishSection: "一键发布",
    publishHint:
      "把二创（或当前工作视频）写入已绑定且有效的平台台账。v1 只做本机分发记录，不向抖音/快手服务器投稿（没有官方第三方上传接口）。",
    derivedPublish: "下载发布报告",
  },
  ask: {
    lede: "基于项目知识库（Milvus 向量库 + 本地大模型）回答问题，答案附引用来源。",
    placeholder: "向知识库提问，例如：有哪些职场资产包？分别多少钱？",
  },
};

const zhHant: UiCopy = {
  feed: {
    kicker: "已發布",
    headline: "影片筆記列表",
    lede: "瀏覽已完成轉寫、翻譯並匯出的影片筆記。",
    cta: "查看筆記",
    empty: "還沒有已發布的筆記。請先在首頁處理一條影片。",
    searchPlaceholder: "搜尋標題、主題或要點…",
    colTitle: "標題",
    colPlatform: "平台",
    colTopic: "主題",
    colHighlights: "要點預覽",
    colPoints: "條數",
    colAction: "操作",
    pagePrev: "上一頁",
    pageNext: "下一頁",
    pageStatus: "第 {from}–{to} 條，共 {total} 條",
    perPage: "每頁",
    noResults: "沒有匹配的筆記。",
  },
  article: {
    notes: "總結",
    overview: "總體總結",
    openNotes: "查看筆記",
    closeNotes: "關閉",
    focuses: "重點",
    keyPoints: "要點",
    hardPoints: "難點",
    source: "來源",
    feed: "列表",
    captions: "逐句字幕",
    downloadCaptions: "下載字幕（.srt）",
    downloadNotes: "下載筆記",
    downloadClips: "下載 MP4 片段",
    downloadClipItem: "片段",
    downloadClipFile: "下載",
    previewClip: "預覽",
    viewFrame: "點擊查看",
    mediaPreview: "畫面",
    mediaOpen: "查看畫面",
    mediaGifKind: "動圖",
    mediaTabClip: "MP4",
    mediaBack: "返回列表",
    mediaCount: "{n} 項",
    clipsEmpty:
      "還沒有 MP4 片段。請在首頁「② MP4 片段」填寫開始/結束秒（例如 10→20），再點生成。上方「① GIF 時長」不會出現在這裡。",
    segmentCaptions: "該片段對應字幕",
    downloadSegmentCaptions: "下載片段字幕（.srt）",
    disclaimer: "本站只整理文字步驟與短片段，不託管完整原影片。完整示範請到原平台。",
    watchYoutube: "去 YouTube 觀看",
    watchBilibili: "去 B 站觀看",
    watchDouyin: "去抖音觀看",
    unlock: "看一段廣告，解鎖完整筆記",
    unlocking: "廣告播放中",
    lockedHint: "重點和難點在廣告後開放。",
    morePoints: "還有 {n} 條",
    remixPlay: "9:16 二創（字幕疊在畫面上，不燒進影片）",
  },
  nav: {
    language: "語言",
    brand: "Vitual",
    feed: "列表",
    ask: "問問一桌",
    themeLight: "淺色",
    themeDark: "深色",
    themeToLight: "切換到淺色",
    themeToDark: "切換到深色",
  },
  try: {
    ...zh.try,
    kicker: "處理單條影片",
    headline: "貼上連結，或上傳本機影片",
    lede: "貼上連結或上傳影片，自動轉寫並生成分語言筆記、字幕與步驟片段。支援 B 站、YouTube、抖音及本機 mp4/mov；本地處理，約 30–90 分鐘。",
    tabUrl: "貼上連結",
    tabUpload: "上傳檔案",
    urlLabel: "影片連結",
    fileLabel: "影片檔案",
    submit: "生成筆記",
    reset: "重置",
    openNotes: "查看筆記",
    batchJobsTitle: "批量任務",
    batchSummary: "共 {total} 條 · {running} 進行中 · {queued} 排隊",
    progressNow: "當前：{stage}",
    jobCancel: "取消",
    jobRetry: "重跑",
    queuePause: "暫停佇列",
    queueResume: "繼續佇列",
    queuePaused: "佇列已暫停 — 當前任務完成後不再啟動新任務。",
    statusCancelled: "已取消",
    stageFailed: "失敗",
    stageCancelled: "已取消",
    modulesSection: "本次工作流",
    moduleTranslate: "字幕翻譯",
    moduleNotes: "結構化筆記",
    moduleClips: "切片",
    modulesHint: "點頂部開關選擇本輪工作流；灰色為未上線。",
    workflowRailSource: "來源",
    workflowRailTranslate: "翻譯",
    workflowRailNotes: "筆記",
    workflowRailClips: "切片",
    workflowRailEnhance: "增強",
    workflowRailCompress: "壓縮",
    workflowRailConcat: "拼接",
    workflowRailRemix: "二創",
    workflowRailPublish: "發布",
    workflowRailSoon: "待上線",
    workflowRailHint: "點選本輪要跑的工作流。",
  },
  ask: {
    lede: "基於專案知識庫（Milvus 向量庫 + 本地大模型）回答問題，答案附引用來源。",
    placeholder: "向知識庫提問，例如：有哪些職場資產包？分別多少錢？",
  },
};

function overlay(partial: {
  feed?: Partial<FeedCopy>;
  article?: Partial<ArticleCopy>;
  nav?: Partial<NavCopy>;
  try?: Partial<TryCopy>;
  ask?: Partial<AskCopy>;
}): UiCopy {
  return {
    feed: { ...en.feed, ...partial.feed },
    article: { ...en.article, ...partial.article },
    nav: { ...en.nav, ...partial.nav },
    try: { ...en.try, ...partial.try },
    ask: { ...en.ask, ...partial.ask },
  };
}

export const ui = {
  zh,
  en,
  "zh-Hant": zhHant,
  ru: overlay({
    feed: {
      kicker: "Library",
      headline: "Published video notes",
      cta: "Open notes",
    },
    article: {
      notes: "Конспект",
      focuses: "Главное",
      keyPoints: "Ключевые пункты",
      hardPoints: "Сложные места",
      source: "Источник",
      feed: "Библиотека",
      captions: "Субтитры",
      watchYoutube: "Смотреть демо на YouTube",
      watchBilibili: "Смотреть демо на Bilibili",
      unlock: "Короткая реклама — полный конспект",
      unlocking: "Реклама",
      morePoints: "ещё {n}",
    },
    nav: { language: "Язык", feed: "Библиотека" },
  }),
  ja: overlay({
    feed: {
      kicker: "Library",
      headline: "Published video notes",
      cta: "Open notes",
    },
    article: {
      notes: "まとめ",
      focuses: "重点",
      keyPoints: "要点",
      hardPoints: "注意点",
      source: "出典",
      feed: "一覧",
      captions: "字幕",
      watchYoutube: "YouTubeで実演を見る",
      watchBilibili: "ビリビリで実演を見る",
      unlock: "広告を見て全文を開く",
      unlocking: "広告再生中",
      morePoints: "ほか {n} 件",
    },
    nav: { language: "言語", feed: "一覧" },
  }),
  ko: overlay({
    feed: {
      kicker: "Library",
      headline: "Published video notes",
      cta: "Open notes",
    },
    article: {
      notes: "정리",
      focuses: "핵심",
      keyPoints: "요점",
      hardPoints: "주의점",
      source: "출처",
      feed: "목록",
      captions: "자막",
      watchYoutube: "YouTube에서 시연 보기",
      watchBilibili: "Bilibili에서 시연 보기",
      unlock: "광고를 보면 전체 노트 잠금 해제",
      unlocking: "광고 재생 중",
      morePoints: "{n}개 더",
    },
    nav: { language: "언어", feed: "목록" },
  }),
  pt: overlay({
    feed: { cta: "Ver os passos", },
    article: {
      notes: "Notas",
      focuses: "O essencial",
      keyPoints: "Pontos-chave",
      hardPoints: "Atenção",
      source: "Fonte",
      feed: "Início",
      captions: "Legendas",
      watchYoutube: "Ver a demonstração no YouTube",
      watchBilibili: "Ver a demonstração no Bilibili",
      unlock: "Ver um anúncio para abrir as notas",
      unlocking: "Anúncio",
      morePoints: "mais {n}",
    },
    nav: { language: "Idioma"},
  }),
  de: overlay({
    feed: { cta: "Schritte lesen", },
    article: {
      notes: "Notizen",
      focuses: "Kern",
      keyPoints: "Kernpunkte",
      hardPoints: "Fallstricke",
      source: "Quelle",
      feed: "Start",
      captions: "Untertitel",
      watchYoutube: "Demo auf YouTube ansehen",
      watchBilibili: "Demo auf Bilibili ansehen",
      unlock: "Kurze Werbung — volle Notizen",
      unlocking: "Werbung",
      morePoints: "noch {n}",
    },
    nav: { language: "Sprache"},
  }),
  es: overlay({
    feed: { cta: "Ver los pasos", },
    article: {
      notes: "Notas",
      focuses: "Lo esencial",
      keyPoints: "Puntos clave",
      hardPoints: "Ojo",
      source: "Fuente",
      feed: "Inicio",
      captions: "Subtítulos",
      watchYoutube: "Ver la demo en YouTube",
      watchBilibili: "Ver la demo en Bilibili",
      unlock: "Un anuncio corto desbloquea las notas",
      unlocking: "Anuncio",
      morePoints: "{n} más",
    },
    nav: { language: "Idioma"},
  }),
  fr: overlay({
    feed: { cta: "Lire les étapes", },
    article: {
      notes: "Notes",
      focuses: "L’essentiel",
      keyPoints: "Points clés",
      hardPoints: "Pièges",
      source: "Source",
      feed: "Accueil",
      captions: "Sous-titres",
      watchYoutube: "Voir la démo sur YouTube",
      watchBilibili: "Voir la démo sur Bilibili",
      unlock: "Une pub courte déverrouille les notes",
      unlocking: "Publicité",
      morePoints: "{n} de plus",
    },
    nav: { language: "Langue"},
  }),
  ar: overlay({
    feed: { cta: "اقرأ الخطوات", },
    article: {
      notes: "الملخص",
      focuses: "المحور",
      keyPoints: "النقاط الرئيسية",
      hardPoints: "تنبيهات",
      source: "المصدر",
      feed: "الرئيسية",
      captions: "الترجمة",
      watchYoutube: "شاهد العرض على يوتيوب",
      watchBilibili: "شاهد العرض على بيليبيلي",
      unlock: "إعلان قصير لفتح الملاحظات",
      unlocking: "الإعلان",
      morePoints: "{n} إضافية",
    },
    nav: { language: "اللغة"},
  }),
  hi: overlay({
    feed: { cta: "चरण देखें", },
    article: {
      notes: "सार",
      focuses: "मुख्य बातें",
      keyPoints: "मुख्य बिंदु",
      hardPoints: "सावधानियाँ",
      source: "स्रोत",
      feed: "होम",
      captions: "उपशीर्षक",
      watchYoutube: "YouTube पर डेमो देखें",
      watchBilibili: "Bilibili पर डेमो देखें",
      unlock: "छोटा विज्ञापन — पूरी नोट्स",
      unlocking: "विज्ञापन",
      morePoints: "और {n}",
    },
    nav: { language: "भाषा"},
  }),
  id: overlay({
    feed: { cta: "Lihat langkah", },
    article: {
      notes: "Catatan",
      focuses: "Inti",
      keyPoints: "Poin utama",
      hardPoints: "Yang mudah salah",
      source: "Sumber",
      feed: "Beranda",
      captions: "Teks",
      watchYoutube: "Tonton demo di YouTube",
      watchBilibili: "Tonton demo di Bilibili",
      unlock: "Iklan singkat membuka catatan lengkap",
      unlocking: "Iklan",
      morePoints: "{n} lagi",
    },
    nav: { language: "Bahasa"},
  }),
  vi: overlay({
    feed: { cta: "Xem các bước", },
    article: {
      notes: "Ghi chú",
      focuses: "Trọng tâm",
      keyPoints: "Điểm chính",
      hardPoints: "Dễ sai",
      source: "Nguồn",
      feed: "Trang chủ",
      captions: "Phụ đề",
      watchYoutube: "Xem demo trên YouTube",
      watchBilibili: "Xem demo trên Bilibili",
      unlock: "Xem quảng cáo để mở ghi chú đầy đủ",
      unlocking: "Quảng cáo",
      morePoints: "còn {n}",
    },
    nav: { language: "Ngôn ngữ"},
  }),
  th: overlay({
    feed: { cta: "ดูขั้นตอน", },
    article: {
      notes: "บันทึก",
      focuses: "จุดสำคัญ",
      keyPoints: "ประเด็นสำคัญ",
      hardPoints: "จุดที่พลาดง่าย",
      source: "แหล่งที่มา",
      feed: "หน้าแรก",
      captions: "คำบรรยาย",
      watchYoutube: "ดูเดโมบน YouTube",
      watchBilibili: "ดูเดโมบน Bilibili",
      unlock: "โฆษณาสั้นเพื่อปลดล็อกบันทึกเต็ม",
      unlocking: "โฆษณา",
      morePoints: "อีก {n}",
    },
    nav: { language: "ภาษา"},
  }),
  tr: overlay({
    feed: { cta: "Adımları gör", },
    article: {
      notes: "Notlar",
      focuses: "Özü",
      keyPoints: "Önemli noktalar",
      hardPoints: "Dikkat",
      source: "Kaynak",
      feed: "Ana sayfa",
      captions: "Altyazılar",
      watchYoutube: "Demoyu YouTube’da izle",
      watchBilibili: "Demoyu Bilibili’de izle",
      unlock: "Kısa reklam tam notları açar",
      unlocking: "Reklam",
      morePoints: "{n} tane daha",
    },
    nav: { language: "Dil"},
  }),
} as const satisfies Record<Locale, UiCopy>;

export function t(locale: Locale): UiCopy {
  return ui[locale];
}

export function isRtl(locale: Locale): boolean {
  return locale === "ar";
}

export function interpolate(template: string, n: number): string {
  return template.replace("{n}", String(n));
}

export function fillCopy(
  template: string,
  vars: Record<string, string | number>
): string {
  let out = template;
  for (const [key, val] of Object.entries(vars)) {
    out = out.replaceAll(`{${key}}`, String(val));
  }
  return out;
}
