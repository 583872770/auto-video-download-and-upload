[CmdletBinding()]
param(
    [ValidateSet('setup', 'tests', 'web', 'download', 'youtube-login')]
    [string]$Mode = 'web',
    [string]$Config,
    [string]$Url,
    [switch]$UseWifi,
    [int]$Port = 5000
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$oldProxy = $env:VIDEO_BROWSER_PROXY
$oldUtf8 = $env:PYTHONUTF8
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:PYTHONUTF8 = '1'
    if ($Mode -eq 'setup' -and -not (Test-Path -LiteralPath $python)) {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 虚拟环境创建失败。' }
    }
    if (-not (Test-Path -LiteralPath $python)) {
        throw '请先执行 .\run.ps1 -Mode setup'
    }
    if ($UseWifi) {
        $stateFile = Join-Path $PSScriptRoot '.cache/network/wlan-xray-state.json'
        $listener = Get-NetTCPConnection -LocalPort 21931 -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            if (-not (Test-Path -LiteralPath $stateFile)) { throw '21931 端口被其他进程占用。' }
            $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
            if ($listener.OwningProcess -ne $state.processId) { throw '项目代理进程状态不匹配。' }
        } else {
            & (Join-Path $PSScriptRoot 'scripts/start_wlan_proxy.ps1')
        }
        $env:VIDEO_BROWSER_PROXY = 'http://127.0.0.1:21931'
    }
    $configArgs = @()
    if ($Config) { $configArgs = @('--config', $Config) }
    switch ($Mode) {
        'setup' { & $python -m pip install -r requirements.lock.txt }
        'tests' { & $python -m unittest discover -s tests -v }
        'web' { & $python app.py @configArgs --port $Port }
        'download' {
            $urlArgs = @()
            if ($Url) { $urlArgs = @($Url) }
            & $python webapp/downloader.py @configArgs @urlArgs
        }
        'youtube-login' { & $python scripts/login_youtube.py @configArgs }
    }
    $resultCode = $LASTEXITCODE
} finally {
    if ($null -eq $oldProxy) { Remove-Item Env:VIDEO_BROWSER_PROXY -ErrorAction SilentlyContinue }
    else { $env:VIDEO_BROWSER_PROXY = $oldProxy }
    if ($null -eq $oldUtf8) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue }
    else { $env:PYTHONUTF8 = $oldUtf8 }
    Pop-Location
}
exit $resultCode
