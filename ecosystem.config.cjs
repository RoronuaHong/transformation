/** PM2: FastAPI ops + GPU dehardsub jobs. */
const path = require("path");

const pipeline = path.join(__dirname, "subtitle_pipeline");
const py = path.join(pipeline, ".venv", "Scripts", "python.exe");

module.exports = {
  apps: [
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
        VITUAL_REQUIRE_GPU: "1",
      },
    },
    {
      name: "vitual-dehardsub-gpu",
      script: py,
      args: "-u _run_BV1Sqgp6kEPN_gpu.py",
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
        VITUAL_ORT_THREADS: "4",
      },
    },
  ],
};
