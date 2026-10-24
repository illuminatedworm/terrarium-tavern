param([int]$Port = 8000, [string]$Data = 'data', [switch]$Desktop)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'tools/desktop-env.ps1')
if (!$tavernPortable -and !(Test-Path -LiteralPath 'dist/index.html') -and !(Test-Path -LiteralPath 'src/tavern/static/web/index.html')) { throw '请先运行 build.ps1' }
$tavernClient = [System.Net.Sockets.TcpClient]::new()
try {
    $tavernClient.Connect('127.0.0.1', $Port)
    throw "TAVERN_PORT_IN_USE: 端口 $Port 已被使用；请先运行 stop.ps1，或使用 -Port 选择另一端口。"
} catch [System.Net.Sockets.SocketException] {
    # No listener: safe to launch. Uvicorn still detects a subsequent race.
} finally {
    $tavernClient.Dispose()
}
# Keep the result as an array: splatting a scalar string passes one argument per character.
$tavernOptions = @(if ($Desktop) { '--desktop' })
Write-Host "Python: $tavernPython"
& $tavernPython -m tavern --port $Port --data $Data @tavernOptions
if ($LASTEXITCODE) { throw "启动失败（退出码 $LASTEXITCODE）" }
