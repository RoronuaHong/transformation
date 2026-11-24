/**
 * PM2: Vitual 常驻服务。
 *
 * 端口规划（Vitual 专属 34xx 段，避开其它项目）：
 *   3401  vitual-hub-web   transform Next UI（/zh/hub，产物 .next-prod）
 *   3402  vitual-hub-api   materials_hub（/api/*，经 UI 的 /hub-api 代理）
 *   3403  npm run dev      本地开发端口（用 .next，不与生产抢端口/产物）
 *   8901  vitual-api       subtitle_pipeline ops-api
 *
 * Windows 无黑框：Python 应用一律 pythonw.exe；子进程闪 CMD 靠
 * subtitle_pipeline/win_no_console.py（入口 import），不是再挂更多 PM2。
 * 规则：.cursor/rules/windows-no-console-flash.mdc
 *
 * 只启动 hub 两个服务（不误启 GPU 去字幕长任务）：
 *   pm2 start ecosystem.config.cjs --only vitual-hub-api,vitual-hub-web
 *   pm2 save
 * 或直接运行 start_suite.ps1（幂等）。
 *
 * 其它项目已占用，勿用：FunUnion 3000/8000-8005、admin 3001、
 * MineTools 5180/8015、vitual-api 8901。
 */
const path = require("path");

const pipeline = path.join(__dirname, "subtitle_pipeline");
const transformDir = path.join(__dirname, "transform");
const hubDir = path.join(__dirname, "materials_hub");
const py = path.join(pipeline, ".venv", "Scripts", "python.exe");
// pythonw: no console window for the PM2 parent (children still need CREATE_NO_WINDOW).
const pyw = path.join(pipeline, ".venv", "Scripts", "pythonw.exe");
const hubPy = process.env.VITUAL_HUB_PYTHON || pyw;
const nodeBin = process.env.VITUAL_NODE_BIN || "C:\\Program Files\\nodejs\\node.exe";

module.exports = {
  apps: [
    {
      name: "vitual-hub-api",
      script: hubPy,
      args: "_run_hub.py",
      interpreter: "none",
      cwd: hubDir,
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 3000,
      max_memory_restart: "1G",
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        VITUAL_HUB_PORT: "3402",
        VITUAL_HUB_HOST: "127.0.0.1",
        VITUAL_RERANK_MODEL: "__none__",
        VITUAL_HUB_EXT_ROOTS:
          "C:\\Users\\msi\\Desktop;C:\\Users\\msi\\Videos;C:\\Users\\msi\\Downloads",
      },
    },
    {
      name: "vitual-hub-web",
      script: nodeBin,
      args: "node_modules/next/dist/bin/next start -p 3401",
      interpreter: "none",
      cwd: transformDir,
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 3000,
      max_memory_restart: "1G",
      env: {
        NODE_ENV: "production",
        // 生产构建目录与 next dev 的 .next 隔离，见 next.config.ts 的 distDir。
        VITUAL_DIST_DIR: ".next-prod",
        VITUAL_HUB_UPSTREAM: "http://127.0.0.1:3402",
        NEXT_PUBLIC_HUB_UPSTREAM: "http://127.0.0.1:3402",
      },
    },
    {
      name: "vitual-api",
      script: path.join(pipeline, ".venv", "Scripts", "pythonw.exe"),
      args: "-m uvicorn api.app:app --host 127.0.0.1 --port 8901 --log-level info",
      interpreter: "none",
      cwd: pipeline,
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 3000,
      max_memory_restart: "1G",
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        VITUAL_REQUIRE_GPU: "1",
        VITUAL_HUB_SYNC: "0",
        // 2026-11-24 黄金标杆工作流（用户验收 gold50 / src_0001_final2）。
        // 顶栏锁：regions 原框 + ns_box_wipe(feather6,radius5)；禁止 expand_static_overlay_box。
        // 见 dehardsub_gold_workflow.top_gold_contract() / eval/gold/dehardsub_gold_workflow/。
        VITUAL_DEHARDSUB_GOLD_WORKFLOW: "1",
        VITUAL_DEHARDSUB_GOLD_WORKERS: "4",
        // 关键帧库只服务中景/对白 donor；顶栏仍走上述锁，不贴 donor。
        VITUAL_DEHARDSUB_GOLD_KF: "1",
        VITUAL_DEHARDSUB_GOLD_KF_STRIDE: "0.5",
        VITUAL_DEHARDSUB_GOLD_MASK: "1",
        VITUAL_DEHARDSUB_REFINE: "0",
        VITUAL_DIALOGUE_SAFETY_OFF: "1",
        // hybrid 旁路已由 GOLD_WORKFLOW 接管；下列保持关闭避免旧路径干扰。
        VITUAL_DEHARDSUB_FILL_V2: "0",
        VITUAL_DEHARDSUB_WM_ADAPTIVE: "0",
        VITUAL_DEHARDSUB_WM_VLM: "0",
        VITUAL_DEHARDSUB_WM_FULL_QA: "0",
      },
    },
    {
      name: "vitual-dehardsub-gpu",
      script: pyw,
      args: `-u "${path.join(pipeline, "_run_BV1Sqgp6kEPN_gpu.py")}"`,
      interpreter: "none",
      cwd: pipeline,
      instances: 1,
      exec_mode: "fork",
      autorestart: false,
      watch: false,
      max_memory_restart: "6G",
      out_file: path.join(
        pipeline,
        "downloads/mode-renders/BV1Sqgp6kEPN_gpu/run.out.log"
      ),
      error_file: path.join(
        pipeline,
        "downloads/mode-renders/BV1Sqgp6kEPN_gpu/run.err.log"
      ),
      merge_logs: true,
      env: {
        PYTHONUNBUFFERED: "1",
        VITUAL_REQUIRE_GPU: "1",
        VITUAL_HUB_SYNC: "0",
        VITUAL_ORT_THREADS: "4",
        VITUAL_GLYPHFILL_BATCH: "8",
      },
    },
  ],
};
