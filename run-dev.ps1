$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw '请先运行 .\setup.ps1' }
& .venv/Scripts/python.exe -m tavern --dev
if ($LASTEXITCODE) { throw "开发窗口已退出（退出码 $LASTEXITCODE）" }
