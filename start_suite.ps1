$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
function Test-Port($p){ (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue) -ne $null }
if (Test-Port 3000){ Write-Host skip-transform } else { Write-Host start-transform; Start-Process -WindowStyle Hidden npm -WorkingDirectory (Join-Path $ROOT transform) -ArgumentList run,start }
Start-Sleep -Seconds 3
Set-Location (Join-Path $ROOT materials_hub)
python server.py
