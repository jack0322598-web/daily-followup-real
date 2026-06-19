param(
    [string]$TaskName = "NewsScraperDailyUpdate",
    [string]$TaskTime = "07:00",
    [string]$WorkflowScript = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = if ($WorkflowScript) { $WorkflowScript } else { Join-Path $Workspace "trigger_github_workflow_dispatch.ps1" }

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Dispatch script not found: $ScriptPath"
}

$triggerTime = [DateTime]::ParseExact($TaskTime, "HH:mm", $null)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

if ($DryRun) {
    Write-Output "TaskName: $TaskName"
    Write-Output "TaskTime: $TaskTime"
    Write-Output "ScriptPath: $ScriptPath"
    exit 0
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Trigger GitHub workflow_dispatch for the news scraper every morning at 7 AM." `
    -Force | Out-Null

Write-Output "Scheduled task '$TaskName' registered to run $ScriptPath at $TaskTime every day."
