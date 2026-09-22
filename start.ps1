param([int]$Port = 8000, [string]$Data = 'data')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw '请先运行 .\setup.ps1' }
if (!(Test-Path -LiteralPath 'dist/index.html') -and !(Test-Path -LiteralPath 'src/tavern/static/web/index.html')) { throw '请先运行 npm run build' }
$tavernClient = [System.Net.Sockets.TcpClient]::new()
try {
    $tavernClient.Connect('127.0.0.1', $Port)
    throw "TAVERN_PORT_IN_USE: 端口 $Port 已被使用；请先运行 stop.ps1，或使用 -Port 选择另一端口。"
} catch [System.Net.Sockets.SocketException] {
    # No listener: safe to launch. Uvicorn still detects a subsequent race.
} finally {
    $tavernClient.Dispose()
}
& .venv/Scripts/python.exe -m tavern --port $Port --data $Data
if ($LASTEXITCODE) { throw "启动失败（退出码 $LASTEXITCODE）" }
