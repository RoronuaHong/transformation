export type Product = {
  id: string
  name: string
  tagline: string
  price: number
  compareAt?: number
  category: string
  badge?: string
  includes: string[]
  delivery: string
  audience: string
  updated: string
}

export const categories = [
  '全部',
  '汇报演示',
  '表格看板',
  '求职成长',
  '工作流合集',
] as const

export const products: Product[] = [
  {
    id: 'weekly-report-kit',
    name: '周报 / 月报 PPT 工具包',
    tagline: '12 套可改结构 + 指标页与结论页写法，周一早上直接套。',
    price: 19.9,
    compareAt: 39.9,
    category: '汇报演示',
    badge: '热销',
    includes: [
      '周报结构 6 套（进度型 / 问题型 / 数据型）',
      '月报结构 6 套（复盘 / 规划 / 跨部门对齐）',
      '一页结论话术库（可复制）',
      '配色与字体规范页',
    ],
    delivery: '付款后网盘自动发货（PPTX + PDF 预览）',
    audience: '需要固定节奏汇报的职场人',
    updated: '2026-08',
  },
  {
    id: 'performance-review-pack',
    name: '述职汇报全流程包',
    tagline: '从业绩叙事到答辩问答，一份包走完述职季。',
    price: 39.9,
    compareAt: 79,
    category: '汇报演示',
    badge: '推荐',
    includes: [
      '述职主 PPT（30 页可裁剪）',
      '业绩故事线框架（STAR / 结果-方法-复盘）',
      '答辩高频问题与参考答法 40 题',
      '领导视角检查清单',
    ],
    delivery: '网盘链接 + 在线目录页',
    audience: '准备季度/年度述职的专员到经理',
    updated: '2026-08',
  },
  {
    id: 'excel-ops-dashboard',
    name: 'Excel 经营看板模板包',
    tagline: '销售、费用、人效三张看板，改数据即出图。',
    price: 29.9,
    category: '表格看板',
    includes: [
      '销售漏斗看板（含示例数据）',
      '费用与预算对照表',
      '人效周报自动汇总表',
      '图表样式与打印设置说明',
    ],
    delivery: 'XLSX 源文件 + 使用短视频说明（网盘）',
    audience: '运营、店长、小团队负责人',
    updated: '2026-07',
  },
  {
    id: 'resume-interview-kit',
    name: '求职简历 + 面试题库包',
    tagline: '简历模板 8 套 + 行为面试题库，投递前一晚改完。',
    price: 19.9,
    category: '求职成长',
    includes: [
      '中英文简历模板各 4 套（Word）',
      '项目经历改写示例 20 条',
      '行为面试题库（分类标注）',
      'Offer 对比评分表',
    ],
    delivery: '压缩包网盘发货',
    audience: '校招 / 社招求职者',
    updated: '2026-08',
  },
  {
    id: 'onboarding-30d',
    name: '新人入职 30 天工作流',
    tagline: '日计划、周对齐、关系地图与复盘模板一次配齐。',
    price: 49.9,
    compareAt: 99,
    category: '工作流合集',
    badge: '合集',
    includes: [
      '30 天日清单（可打印 / 可填 Excel）',
      '岗位知识地图模板',
      '向上管理 1:1 议程',
      '30 天复盘 PPT + 邮件模板',
    ],
    delivery: '文件夹结构打包，网盘一次交付',
    audience: '新人本人或带教 mentor',
    updated: '2026-08',
  },
  {
    id: 'meeting-notes-os',
    name: '会议纪要与行动项 OS',
    tagline: '会前议题、会中记录、会后追踪同一套字段。',
    price: 12.9,
    category: '工作流合集',
    includes: [
      '会议纪要模板（Notion / 飞书 / Word 三版）',
      '行动项看板字段说明',
      '会后 15 分钟收口清单',
    ],
    delivery: '多格式文件网盘发货',
    audience: '项目经理、助理、频繁开会的角色',
    updated: '2026-06',
  },
]
