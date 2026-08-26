/** PM2: FastAPI ops. This PC already has mongod on 27018 — do not start a second one. */
const path = require("path");

const pipeline = path.join(__dirname, "subtitle_pipeline");

module.exports = {
  apps: [
    {
      name: "vitual-api",
      script: path.join(pipeline, ".venv", "Scripts", "python.exe"),
      args: "-m uvicorn api.app:app --host 127.0.0.1 --port 8800 --log-level info",
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
      },
    },
  ],
};

