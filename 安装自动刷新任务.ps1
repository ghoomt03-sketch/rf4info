$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$runner = Join-Path $projectRoot 'scheduled_refresh.py'
$publisher = Join-Path $projectRoot 'publish_public_snapshot.py'

if (-not (Test-Path -LiteralPath $python)) {
    throw '未找到项目虚拟环境后台解释器，请先运行“启动俄钓4筛选器.bat”。'
}

$plans = @(
    @{ Name = 'RF4Local-Fast-Catches'; Script = $runner; Arguments = '--profile fast'; Profile = 'fast'; Minutes = 15; StartOffset = 2 },
    @{ Name = 'RF4Local-Hourly-RF4DB'; Script = $runner; Arguments = '--profile hourly'; Profile = 'hourly'; Minutes = 60; StartOffset = 7 },
    @{ Name = 'RF4Local-SixHour-Sources'; Script = $runner; Arguments = '--profile six-hour'; Profile = 'six-hour'; Minutes = 360; StartOffset = 12 },
    @{ Name = 'RF4Local-Publish-Public'; Script = $publisher; Arguments = ''; Profile = 'publish'; Minutes = 60; StartOffset = 20 }
)

foreach ($plan in $plans) {
    $action = New-ScheduledTaskAction -Execute $python `
        -Argument (('"{0}" {1}' -f $plan.Script, $plan.Arguments).Trim()) `
        -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($plan.StartOffset) `
        -RepetitionInterval (New-TimeSpan -Minutes $plan.Minutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType S4U -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $plan.Name -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal `
            -Description '俄罗斯钓鱼4公开数据自动刷新与脱敏发布；非交互后台运行，不绕过验证或访问控制。' `
            -Force -ErrorAction Stop | Out-Null
    } catch {
        Register-ScheduledTask -TaskName $plan.Name -Action $action -Trigger $trigger `
            -Settings $settings `
            -Description '俄罗斯钓鱼4公开数据自动刷新与脱敏发布；无控制台并隐藏专用浏览器，不绕过验证或访问控制。' `
            -Force -ErrorAction Stop | Out-Null
    }
}

$state = @{
    installed = $true
    installed_at = (Get-Date).ToString('o')
    tasks = $plans | ForEach-Object { @{ name = $_.Name; interval_minutes = $_.Minutes; profile = $_.Profile } }
}
$statePath = Join-Path $projectRoot 'data\scheduler_state.json'
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Host '已安装自动任务：渔获15分钟、RF4DB天气/收益1小时、其余来源及历史补采6小时、脱敏公开数据每小时发布。'
