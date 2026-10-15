[CmdletBinding()]
param(
    [string]$InterfaceAlias = 'WLAN',
    [int]$Port = 21931,
    [string]$XrayPath = (Join-Path $env:USERPROFILE 'Desktop/v2rayN-windows-64/bin/xray/xray.exe')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $XrayPath -PathType Leaf)) {
    throw '找不到已安装的 Xray，请通过 -XrayPath 指定。'
}
$addresses = @(Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 |
    Where-Object { $_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '169.254.*' })
if ($addresses.Count -ne 1) {
    throw '需要所选网卡有且只有一个可用 IPv4 地址。'
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "本地端口 $Port 已占用，未修改已有进程。"
}
$repoDirectory = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $repoDirectory '.cache/network'
New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
$configurationPath = Join-Path $stateDirectory 'wlan-xray.json'
$configuration = @{
    log = @{ loglevel = 'warning' }
    inbounds = @(@{
        tag = 'project-only'; listen = '127.0.0.1'; port = $Port
        protocol = 'http'; settings = @{}
    })
    outbounds = @(@{
        tag = 'wlan-direct'; protocol = 'freedom'; sendThrough = $addresses[0].IPAddress
        settings = @{ domainStrategy = 'UseIPv4' }
    })
}
$configuration | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configurationPath -Encoding utf8NoBOM
& $XrayPath run -test -c $configurationPath
if ($LASTEXITCODE -ne 0) { throw 'Xray 配置校验失败，未启动。' }
$process = Start-Process -FilePath $XrayPath -ArgumentList @('run', '-c', ('"' + $configurationPath + '"')) `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $stateDirectory 'wlan-xray.stdout.log') `
    -RedirectStandardError (Join-Path $stateDirectory 'wlan-xray.stderr.log')
@{
    processId = $process.Id; executable = $XrayPath; configuration = $configurationPath
    interface = $InterfaceAlias; sourceAddress = $addresses[0].IPAddress; port = $Port
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDirectory 'wlan-xray-state.json') -Encoding utf8NoBOM
Write-Output "Project proxy started: http://127.0.0.1:$Port ; interface=$InterfaceAlias ; PID=$($process.Id)"
Write-Output 'Only clients explicitly using this proxy are affected.'
