# Project skills

产品真源：`trans/工具需求.md`（译 / **笔记** / 切 / 增强 / 压缩 / 拼 / 二创 / 多发）。  
下列 skills 覆盖**已实现**路径；WF-04～08 落地后另增 skill。

| Skill | When | 对应 |
|-------|------|------|
| [main-path](./main-path/SKILL.md) | 译+笔记+导出验收（e2e） | WF-01 · WF-02 + SEO 旁路 |
| [ops-api](./ops-api/SKILL.md) | 日更/后台；工作台 Try | 发现旁路 · `/api/try/*` |
| [daily-discover](./daily-discover/SKILL.md) | Topic/query → queue | 发现旁路 |
| [batch-local-first](./batch-local-first/SKILL.md) | 队列加工；`export-site` 命令细节 | WF-01 · WF-02 |
| [sync-subs](./sync-subs/SKILL.md) | 字幕时间轴 | WF-01 · WF-03 |

MCP A–C：`subtitle_pipeline/vitual_mcp/` · tools + resources `vitual://export/*`

**命名**：语言包名仍是 `--langs site`（16 codes）；前台目录是 **`transform/`**（旧称 `site/`）。不要新建只包 React UI 的 skill。

端口：工作台 `transform/` :3000；admin :3001；API :8800。详见 `trans/技术栈.md`、`trans/README.md`。

## 分流（避免抢触发）

| 用户说法 | 用 |
|----------|-----|
| 主路径 / 验收 / 跑一遍 / 导出站点 / smoke | `main-path` |
| yarn batch / 加工 / 转写 / LLM profile / `export-site` 命令本身 | `batch-local-first` |
| 后台 / 定时 / 告警 / 工作台 / 试一试 / try | `ops-api` |
| discover / inbox / 搜词（只入队） | `daily-discover` |
| 字幕同步 / SRT offset | `sync-subs` |
