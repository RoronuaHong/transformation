export type Tool = {
  id: string
  name: string
  blurb: string
  category: string
  tags: string[]
  pricing: '免费' | '免费+付费' | '付费'
  url: string
  highlight?: boolean
}

export const toolCategories = [
  '全部',
  '写作表达',
  '表格数据',
  '演示设计',
  '会议协作',
  'AI 助手',
] as const

export const tools: Tool[] = [
  {
    id: 'notion',
    name: 'Notion',
    blurb: '文档、数据库、Wiki 一体，适合个人知识与项目台账。',
    category: '会议协作',
    tags: ['文档', '数据库', 'Wiki'],
    pricing: '免费+付费',
    url: 'https://www.notion.so',
    highlight: true,
  },
  {
    id: 'feishu',
    name: '飞书',
    blurb: '文档、多维表格、会议与审批串在一起，国内团队默认选项。',
    category: '会议协作',
    tags: ['协作', '多维表格', '会议'],
    pricing: '免费+付费',
    url: 'https://www.feishu.cn',
    highlight: true,
  },
  {
    id: 'excel',
    name: 'Microsoft Excel',
    blurb: '经营分析与看板的底盘，模板包多基于它交付。',
    category: '表格数据',
    tags: ['表格', '透视', '看板'],
    pricing: '付费',
    url: 'https://www.microsoft.com/excel',
  },
  {
    id: 'google-sheets',
    name: 'Google 表格',
    blurb: '多人同时编辑、轻量看板，适合跨时区小团队。',
    category: '表格数据',
    tags: ['协作表格', '看板'],
    pricing: '免费+付费',
    url: 'https://sheets.google.com',
  },
  {
    id: 'powerpoint',
    name: 'PowerPoint',
    blurb: '职场汇报主战场；一桌模板默认兼容 PPTX。',
    category: '演示设计',
    tags: ['演示', '汇报'],
    pricing: '付费',
    url: 'https://www.microsoft.com/powerpoint',
    highlight: true,
  },
  {
    id: 'canva',
    name: 'Canva',
    blurb: '快速出图与社媒物料，非设计师也能控版式。',
    category: '演示设计',
    tags: ['设计', '社媒'],
    pricing: '免费+付费',
    url: 'https://www.canva.com',
  },
  {
    id: 'gamma',
    name: 'Gamma',
    blurb: '用提纲生成演示草稿，适合赶时间的初稿阶段。',
    category: 'AI 助手',
    tags: ['AI', '演示'],
    pricing: '免费+付费',
    url: 'https://gamma.app',
  },
  {
    id: 'kimi',
    name: 'Kimi',
    blurb: '长文阅读与材料整理，适合把会议纪要压成行动项。',
    category: 'AI 助手',
    tags: ['AI', '长文'],
    pricing: '免费+付费',
    url: 'https://kimi.moonshot.cn',
    highlight: true,
  },
  {
    id: 'tongyi',
    name: '通义千问',
    blurb: '中文职场写作与改稿，周报初稿可先丢给它。',
    category: '写作表达',
    tags: ['AI', '写作'],
    pricing: '免费+付费',
    url: 'https://tongyi.aliyun.com',
  },
  {
    id: 'word',
    name: 'Microsoft Word',
    blurb: '正式公文、简历与制度文档的标准格式。',
    category: '写作表达',
    tags: ['文档', '简历'],
    pricing: '付费',
    url: 'https://www.microsoft.com/word',
  },
  {
    id: 'miro',
    name: 'Miro',
    blurb: '白板研讨与流程梳理，适合工作坊与跨部门对齐。',
    category: '会议协作',
    tags: ['白板', '研讨'],
    pricing: '免费+付费',
    url: 'https://miro.com',
  },
  {
    id: 'figma-slides',
    name: 'Figma Slides',
    blurb: '设计感更强的演示协作，适合对外提案。',
    category: '演示设计',
    tags: ['演示', '协作'],
    pricing: '免费+付费',
    url: 'https://www.figma.com/slides/',
  },
]
