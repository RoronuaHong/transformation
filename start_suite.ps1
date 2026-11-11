$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
function Test-Port($p){ (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue) -ne $null }

# Vitual Hub (UI :3401 / API :3402) 统一由 pm2 托管，见 ecosystem.config.cjs。
# 以前这里 Test-Port 3000（其实是 FunUnion web-admin 的端口）导致 transform 永远被 skip；
# hub 又用 Start-Process 手工拉起游离进程，与 pm2 抢端口、产生重复实例。
# 现在一律走 pm2：已托管则 restart，否则 start，幂等且不会重复。
$eco = Join-Path $ROOT ecosystem.config.cjs
function Ensure-Pm2App($app) {
  pm2 describe $app 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    # 不带 --update-env：保留 ecosystem 里注入的 VITUAL_HUB_PORT / VITUAL_DIST_DIR 等
    pm2 restart $app | Out-Null
    Write-Host "pm2 restart $app"
  } else {
    pm2 start $eco --only $app | Out-Null
    Write-Host "pm2 start $app"
  }
}
Ensure-Pm2App 'vitual-hub-api'
Ensure-Pm2App 'vitual-hub-web'
pm2 save | Out-Null

# 启动知识库服务 (Milvus Lite, 本地文件, 无需 Docker)
$kbVenv = Join-Path $ROOT "kb\.venv\Scripts\python.exe"
$kbServer = Join-Path $ROOT "kb\server.py"
if (-not (Test-Path $kbVenv)) {
  Write-Host "skip-kb (无 venv, 请先在 kb 下 python -m venv .venv && pip install -r requirements.txt)"
} elseif (Test-Port 8910) {
  Write-Host "skip-kb (端口 8910 已占用)"
} else {
  # 预检 milvus-lite 是否已安装（离线环境需先 pip install milvus-lite，否则进程必崩）
  & $kbVenv -c "import milvus_lite" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "skip-kb (kb/.venv 缺 milvus-lite, 在线执行: kb/.venv/Scripts/pip install milvus-lite)"
  } else {
    Write-Host "start-kb"
    Start-Process -WindowStyle Hidden -FilePath $kbVenv -WorkingDirectory (Join-Path $ROOT kb) -ArgumentList $kbServer
  }
}

# 决策服务 (Clef 决策模型, :8911)
# 默认不启动：它会占用数 GB 显存，与 PM2 的 GPU 去字幕任务争夺同一张 6GB 卡；
# 且实测单次约 1.7s，只适合离线/批量兜底（见决策模型文档 §7）。
# 需要时：先 $env:VITUAL_START_DECISION='1' 再运行本脚本。
$decisionLauncher = Join-Path $ROOT "tools\llama.cpp\start_llama.ps1"
if ($env:VITUAL_START_DECISION -ne '1') {
  Write-Host "skip-decision (默认关闭，需时设 `$env:VITUAL_START_DECISION='1')"
} elseif (-not (Test-Path $decisionLauncher)) {
  Write-Host "skip-decision (未找到 tools\llama.cpp\start_llama.ps1)"
} elseif (Test-Port 8911) {
  Write-Host "skip-decision (端口 8911 已占用)"
} else {
  Write-Host "start-decision (:8911)"
  Start-Process -WindowStyle Hidden -FilePath powershell -ArgumentList '-NoProfile','-File',$decisionLauncher,'-ngl','99','-port','8911'
}
