param([string]$Proxy = '', [switch]$LocalRetrieval)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($tavernCommand in @('python', 'npm.cmd', 'node', 'git')) {
    if (!(Get-Command $tavernCommand -ErrorAction SilentlyContinue)) { throw "缺少 $tavernCommand，请先安装 Python、Node.js 22+ 和 Git，并重新打开终端。" }
}
$tavernNodeMajor = [int]((& node --version).TrimStart('v').Split('.')[0])
if ($tavernNodeMajor -lt 22) { throw '需要 Node.js 22 或更新版本。' }
$env:HTTPS_PROXY = $Proxy
$env:HTTP_PROXY = $Proxy
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot '.cache/python'
python -m pip install uv
if ($LASTEXITCODE) { throw 'uv 安装失败' }
if ($LocalRetrieval) { python -m uv sync --frozen --extra retrieval } else { python -m uv sync --frozen }
if ($LASTEXITCODE) { throw 'Python 依赖安装失败' }
npm.cmd ci --registry=https://registry.npmjs.org --proxy=$Proxy --https-proxy=$Proxy
if ($LASTEXITCODE) { throw '前端依赖安装失败' }
npm.cmd run build
if ($LASTEXITCODE) { throw '前端构建失败' }
& .venv/Scripts/python.exe -m playwright install chromium
if ($LASTEXITCODE) { throw 'Chromium 安装失败' }
Write-Host '安装完成。运行 .\start.ps1，然后访问 http://127.0.0.1:8000'
