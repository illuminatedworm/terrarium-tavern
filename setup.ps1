param([string]$Proxy = '', [switch]$LocalRetrieval, [switch]$SkipNodeDownload)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($tavernCommand in @('python', 'git')) {
    if (!(Get-Command $tavernCommand -ErrorAction SilentlyContinue)) { throw "缺少 $tavernCommand，请先安装 Python 和 Git，并重新打开终端。" }
}
# Omitting -Proxy preserves the caller's environment, matching setup.sh.
if ($PSBoundParameters.ContainsKey('Proxy')) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
    Remove-Item Env:ALL_PROXY -ErrorAction SilentlyContinue
}
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot 'desktop-dev/python'
python -m pip install uv
if ($LASTEXITCODE) { throw 'uv 安装失败' }
if ($LocalRetrieval) { python -m uv sync --frozen --extra retrieval } else { python -m uv sync --frozen }
if ($LASTEXITCODE) { throw 'Python 依赖安装失败' }
if (!$SkipNodeDownload) {
    & .venv/Scripts/python.exe tools/desktop_runtime.py node
    if ($LASTEXITCODE) { throw '内置 Node.js 准备失败' }
}
. (Join-Path $PSScriptRoot 'tools/desktop-env.ps1')
foreach ($tavernCommand in @('node', 'npm.cmd')) {
    if (!(Get-Command $tavernCommand -ErrorAction SilentlyContinue)) { throw "缺少 $tavernCommand；重新运行 setup.ps1 下载内置 Node.js。" }
}
$tavernNodeMajor = [int]((& node --version).TrimStart('v').Split('.')[0])
if ($tavernNodeMajor -lt 22) { throw '需要 Node.js 22 或更新版本。' }
$tavernNpmArgs = @('ci', '--registry=https://registry.npmjs.org')
if ($PSBoundParameters.ContainsKey('Proxy')) {
    $tavernNpmArgs += @("--proxy=$Proxy", "--https-proxy=$Proxy")
}
npm.cmd @tavernNpmArgs
if ($LASTEXITCODE) { throw '前端依赖安装失败' }
npm.cmd run build
if ($LASTEXITCODE) { throw '前端构建失败' }
& .venv/Scripts/python.exe -m playwright install chromium
if ($LASTEXITCODE) { throw 'Chromium 安装失败' }
Write-Host '安装完成。运行 .\点击开始.cmd，然后访问 http://127.0.0.1:51273'
