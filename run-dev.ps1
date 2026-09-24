$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (!(Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw '请先运行 .\setup.ps1' }
if (!(Test-Path -LiteralPath 'node_modules/vite/bin/vite.js')) { throw '请先运行 .\setup.ps1' }

$port = 8000
$backendStarted = $false
$backend = $null
$probe = [System.Net.Sockets.TcpClient]::new()
try {
    $probe.Connect('127.0.0.1', $port)
    $portInUse = $true
} catch [System.Net.Sockets.SocketException] {
    $portInUse = $false
} finally {
    $probe.Dispose()
}

try {
    if ($portInUse) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
        } catch {
            throw "端口 $port 已被占用，但无法确认是 Terrarium Tavern 后端。"
        }
        if ($health.runtime -ne 'KohakuTerrarium') { throw "端口 $port 上不是 Terrarium Tavern 后端。" }
        Write-Host "复用已运行的后端（端口 $port）。"
    } else {
        $backend = Start-Process -FilePath (Join-Path $PSScriptRoot '.venv/Scripts/python.exe') `
            -ArgumentList '-m tavern --port 8000 --data data' -WorkingDirectory $PSScriptRoot `
            -WindowStyle Hidden -PassThru
        $backendStarted = $true
        $ready = $false
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 1
                if ($health.runtime -eq 'KohakuTerrarium') { $ready = $true; break }
            } catch {
                # The backend may still be starting.
            }
            $backend.Refresh()
            if ($backend.HasExited) { throw "后端启动失败（退出码 $($backend.ExitCode)）。" }
            Start-Sleep -Milliseconds 250
        }
        if (!$ready) { throw '等待后端启动超时。' }
        Write-Host "后端已启动（端口 $port）。"
    }

    Write-Host '开发页面：http://127.0.0.1:5173'
    & node node_modules/vite/bin/vite.js --host 127.0.0.1 --strictPort
    if ($LASTEXITCODE) { throw "Vite 已退出（退出码 $LASTEXITCODE）。" }
} finally {
    if ($backendStarted) {
        try {
            & (Join-Path $PSScriptRoot 'stop.ps1') -Port $port
        } catch {
            Write-Warning "自动关闭后端失败：$_；可运行 .\stop.ps1 手动关闭。"
        }
    }
}
