param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
} catch {
    Write-Host "端口 $Port 上没有可访问的酒馆服务。"
    exit 0
}
if ($health.runtime -ne 'KohakuTerrarium') { throw '目标不是 Terrarium Tavern，未执行关停。' }
try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/shutdown" -ContentType 'application/json' -Body '{"confirm":true}' -TimeoutSec 5 | Out-Null
    Write-Host '正在关闭服务，未完成的回合将保留草稿。'
} catch {
    # Supports the older, pre-shutdown-endpoint development server after verifying its command.
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (!$listener) { Write-Host '服务已关闭。'; exit 0 }
    $serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($serverProcess.CommandLine -notmatch '(?i)(-m\s+tavern\b|agentic-tavern.*uvicorn)') { throw '无法确认服务进程身份，未强制结束。' }
    Stop-Process -Id $serverProcess.ProcessId
    Write-Host '旧版本服务已关闭。再次启动后将使用正常关停接口。'
}
