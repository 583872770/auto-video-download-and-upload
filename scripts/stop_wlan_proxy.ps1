[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $repoDirectory '.cache/network/wlan-xray-state.json'
if (-not (Test-Path -LiteralPath $statePath)) {
    Write-Output 'No project proxy state found.'
    return
}
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$expectedConfiguration = Join-Path $repoDirectory '.cache/network/wlan-xray.json'
if ([IO.Path]::GetFullPath($state.configuration) -ne [IO.Path]::GetFullPath($expectedConfiguration)) {
    throw '代理状态中的配置文件路径不匹配，未停止进程。'
}
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.processId)"
if ($null -ne $process) {
    if ($process.ExecutablePath -ne $state.executable -or
        -not $process.CommandLine.Contains($state.configuration)) {
        throw 'PID 已被其他进程使用或进程信息不匹配，未停止进程。'
    }
    Stop-Process -Id ([int]$state.processId) -ErrorAction Stop
    Write-Output "Stopped project proxy PID=$($state.processId)"
}
Remove-Item -LiteralPath $statePath
