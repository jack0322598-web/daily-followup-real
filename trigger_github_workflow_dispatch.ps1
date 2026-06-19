param(
    [string]$Date = "",
    [string]$Repository = "jack0322598-web/daily-followup-real",
    [string]$WorkflowFile = "daily-update-v2.yml",
    [string]$Ref = "main",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Workspace "logs"
$LogFile = Join-Path $LogDir ("workflow_dispatch_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $line | Tee-Object -FilePath $LogFile -Append
}

function Get-GitHubAuthHeader {
    if ($env:GITHUB_WORKFLOW_DISPATCH_TOKEN) {
        return @{ Authorization = "Bearer $($env:GITHUB_WORKFLOW_DISPATCH_TOKEN)" }
    }

    if ($env:GH_TOKEN) {
        return @{ Authorization = "Bearer $($env:GH_TOKEN)" }
    }

    if ($env:GITHUB_TOKEN) {
        return @{ Authorization = "Bearer $($env:GITHUB_TOKEN)" }
    }

    $credInput = "protocol=https`nhost=github.com`n`n"
    $credOutput = $credInput | git credential fill
    if (-not $credOutput) {
        throw "No GitHub credentials available from environment variables or git credential manager."
    }

    $username = ""
    $password = ""
    foreach ($line in ($credOutput -split "`n")) {
        if ($line -like "username=*") {
            $username = $line.Substring(9)
        } elseif ($line -like "password=*") {
            $password = $line.Substring(9)
        }
    }

    if (-not $username -or -not $password) {
        throw "GitHub credentials were incomplete."
    }

    $pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${username}:${password}"))
    return @{ Authorization = "Basic $pair" }
}

function Get-DispatchBody {
    $body = @{
        ref = $Ref
    }

    if ($Date) {
        $body.inputs = @{
            news_date = $Date
        }
    }

    return ($body | ConvertTo-Json -Depth 4)
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$headers = Get-GitHubAuthHeader
$headers["User-Agent"] = "news-scraper-dispatch"
$headers["Accept"] = "application/vnd.github+json"

$dispatchUrl = "https://api.github.com/repos/$Repository/actions/workflows/$WorkflowFile/dispatches"
$bodyJson = Get-DispatchBody

Write-Log "Repository: $Repository"
Write-Log "Workflow: $WorkflowFile"
Write-Log "Ref: $Ref"
Write-Log ("Requested date: {0}" -f ($(if ($Date) { $Date } else { "<auto>" })))
Write-Log "Dispatch URL: $dispatchUrl"

if ($DryRun) {
    Write-Log "Dry run only. No dispatch sent."
    Write-Output $bodyJson
    exit 0
}

Invoke-RestMethod -Method Post -Uri $dispatchUrl -Headers $headers -ContentType "application/json" -Body $bodyJson | Out-Null
Write-Log "workflow_dispatch request accepted by GitHub."

$runsUrl = "https://api.github.com/repos/$Repository/actions/workflows/$WorkflowFile/runs?branch=$Ref&event=workflow_dispatch&per_page=5"
$dispatchStartUtc = (Get-Date).ToUniversalTime().AddMinutes(-2)
$matchedRun = $null

for ($attempt = 1; $attempt -le 12; $attempt++) {
    Start-Sleep -Seconds 5
    $response = Invoke-RestMethod -Method Get -Uri $runsUrl -Headers $headers
    foreach ($run in $response.workflow_runs) {
        $createdAt = [DateTime]::Parse($run.created_at).ToUniversalTime()
        if ($createdAt -ge $dispatchStartUtc) {
            $matchedRun = $run
            break
        }
    }

    if ($matchedRun) {
        break
    }
}

if ($matchedRun) {
    Write-Log ("Matched run #{0} status={1} conclusion={2}" -f $matchedRun.run_number, $matchedRun.status, $matchedRun.conclusion)
    Write-Log ("Run URL: {0}" -f $matchedRun.html_url)
} else {
    Write-Log "No newly created workflow_dispatch run was observed within the polling window."
}
