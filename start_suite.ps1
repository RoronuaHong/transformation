$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
function Test-Port($p){ (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue) -ne $null }
if (Test-Port 3000){ Write-Host skip-transform } else { Write-Host start-transform; Start-Process -WindowStyle Hidden -FilePath cmd -WorkingDirectory (Join-Path $ROOT transform) -ArgumentList '/k','npm','run','dev' }

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

Start-Sleep -Seconds 3
Set-Location (Join-Path $ROOT materials_hub)
python server.py
