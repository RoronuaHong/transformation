$ErrorActionPreference = 'Continue'
# 自保护：杀掉其它 dl_clef 旧实例与残留 curl，防止双进程并发写同一文件
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match 'dl_clef' -and $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-Process curl -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$dir = 'd:\MineWeb\2026\Vitual\subtitle_pipeline\models\clef'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$url = 'https://www.modelscope.cn/models/ggml-org/Clef-Flash-GGUF/resolve/master/Clef-Flash-Q4_K_M.gguf'
$dest = Join-Path $dir 'Clef-Flash-Q4_K_M.gguf'
# -C - 断点续传; 循环重试直到文件大小稳定且 curl 退出码为 0
$exit = 1
for ($i = 0; $i -lt 20 -and $exit -ne 0; $i++) {
  & curl.exe -L -C - --retry 3 --retry-delay 3 -sS -o $dest $url
  $exit = $LASTEXITCODE
  if ($exit -ne 0) { Start-Sleep -Seconds 5 }
}
"DONE exit=$exit size=$((Get-Item $dest).Length)" | Out-File (Join-Path $dir 'dl_status.txt') -Encoding utf8
