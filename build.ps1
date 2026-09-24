$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (!(Test-Path -LiteralPath 'node_modules/vite/bin/vite.js')) { throw '请先运行 .\setup.ps1 安装前端依赖。' }

& npm.cmd run build
if ($LASTEXITCODE) { throw "前端构建失败（退出码 $LASTEXITCODE）。" }

Write-Host '构建完成：dist/ 和 src/tavern/static/web/ 已更新。'
