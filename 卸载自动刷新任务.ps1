$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
@('RF4Local-Fast-Catches', 'RF4Local-Hourly-RF4DB', 'RF4Local-SixHour-Sources') | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue
}
$statePath = Join-Path $projectRoot 'data\scheduler_state.json'
if (Test-Path -LiteralPath $statePath) {
    Remove-Item -LiteralPath $statePath -Force
}
Write-Host '已卸载俄罗斯钓鱼4自动刷新任务。'
