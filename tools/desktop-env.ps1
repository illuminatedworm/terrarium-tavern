# Shared by source and portable launchers. Only this process inherits these paths.
$tavernPortable = Test-Path -LiteralPath (Join-Path $PSScriptRoot '../runtime/python/python.exe')
if ($tavernPortable) {
    $tavernPython = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../runtime/python/python.exe'))
    $env:PYTHONNOUSERSITE = '1'
    Remove-Item Env:PYTHONHOME,Env:PYTHONPATH,Env:PLAYWRIGHT_NODEJS_PATH,Env:PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue
} else {
    $tavernPython = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../.venv/Scripts/python.exe'))
    $tavernRuntimes = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../packaging/desktop/runtimes.json') -Raw | ConvertFrom-Json
    $tavernNodeDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ("../desktop-dev/node/" + $tavernRuntimes.node.'windows-x64'.prefix)))
    if (Test-Path -LiteralPath (Join-Path $tavernNodeDirectory 'node.exe')) {
        $env:PATH = $tavernNodeDirectory + [IO.Path]::PathSeparator + $env:PATH
    }
}
if (!(Test-Path -LiteralPath $tavernPython)) { throw '请先运行 setup.ps1，或使用完整的便携发行包。' }
