param(
    [string]$Date = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$SystemPython = "C:\Users\GRAM_\AppData\Local\Programs\Python\Python314\python.exe"
$BundledPython = "C:\Users\GRAM_\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Python = if (Test-Path -LiteralPath $SystemPython) { $SystemPython } elseif (Test-Path -LiteralPath $BundledPython) { $BundledPython } else { "py" }
$NewsDate = if ($Date) { $Date } else { (Get-Date).AddDays(-1).ToString("yyyy-MM-dd") }
$LogDir = Join-Path $Workspace "logs"
$LogFile = Join-Path $LogDir ("scheduled_update_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))
$BackupDir = Join-Path $LogDir ("backup_{0}" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))
$LockFile = Join-Path $Workspace ".update.lock"
$ArchiveFile = Join-Path $Workspace ("archive_{0}.html" -f $NewsDate)
$ProtectedFiles = @(
    (Join-Path $Workspace "index.html"),
    (Join-Path $Workspace "share_index.html"),
    (Join-Path $Workspace "archive_list.js"),
    $ArchiveFile
)

function Backup-ProtectedFiles {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    foreach ($file in $ProtectedFiles) {
        if (Test-Path -LiteralPath $file) {
            Copy-Item -LiteralPath $file -Destination (Join-Path $BackupDir (Split-Path -Leaf $file)) -Force
        }
    }
}

function Restore-ProtectedFiles {
    foreach ($file in $ProtectedFiles) {
        $backup = Join-Path $BackupDir (Split-Path -Leaf $file)
        if (Test-Path -LiteralPath $backup) {
            Copy-Item -LiteralPath $backup -Destination $file -Force
        } elseif (Test-Path -LiteralPath $file) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-GeneratedArchive {
    if (-not (Test-Path -LiteralPath $ArchiveFile)) {
        "Validation failed: archive file was not created." | Tee-Object -FilePath $LogFile -Append
        return $false
    }

    $html = Get-Content -LiteralPath $ArchiveFile -Raw -Encoding UTF8
    $newsCount = ([regex]::Matches($html, 'class="news-card')).Count
    "Validation: news cards found = $newsCount" | Tee-Object -FilePath $LogFile -Append

    if ($newsCount -lt 3) {
        "Validation failed: generated page has too few news cards." | Tee-Object -FilePath $LogFile -Append
        return $false
    }

    if ((Test-Path -LiteralPath $LogFile) -and (Select-String -LiteralPath $LogFile -Pattern "WinError 10013|액세스 권한에 의해 숨겨진 소켓" -Quiet)) {
        "Validation failed: network socket was blocked during collection." | Tee-Object -FilePath $LogFile -Append
        return $false
    }

    return $true
}

if ($DryRun) {
    Write-Output "Workspace: $Workspace"
    Write-Output "Python: $Python"
    Write-Output "NewsDate: $NewsDate"
    Write-Output "LogFile: $LogFile"
    Write-Output "BackupDir: $BackupDir"
    Write-Output "ArchiveFile: $ArchiveFile"
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location -LiteralPath $Workspace

if (Test-Path -LiteralPath $LockFile) {
    $lockAge = (Get-Date) - (Get-Item -LiteralPath $LockFile).LastWriteTime
    if ($lockAge.TotalHours -lt 4) {
        "Another update appears to be running. Lock age: $($lockAge.TotalMinutes.ToString('0.0')) minutes" | Tee-Object -FilePath $LogFile
        exit 3
    }
}

Set-Content -LiteralPath $LockFile -Value (Get-Date).ToString("o") -Encoding UTF8
try {
    Backup-ProtectedFiles
    "Starting scheduled news update at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Tee-Object -FilePath $LogFile
    "Workspace: $Workspace" | Tee-Object -FilePath $LogFile -Append
    "Python: $Python" | Tee-Object -FilePath $LogFile -Append
    "News date: $NewsDate" | Tee-Object -FilePath $LogFile -Append
    "Backup dir: $BackupDir" | Tee-Object -FilePath $LogFile -Append

    if ($Python -eq "py") {
        & py (Join-Path $Workspace "main.py") --date $NewsDate 2>&1 | Tee-Object -FilePath $LogFile -Append
    } else {
        & $Python (Join-Path $Workspace "main.py") --date $NewsDate 2>&1 | Tee-Object -FilePath $LogFile -Append
    }
    $exitCode = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 0 }

    if (-not (Test-Path -LiteralPath $ArchiveFile)) {
        "Expected archive was not created: $ArchiveFile" | Tee-Object -FilePath $LogFile -Append
        exit 2
    }

    if (-not (Test-GeneratedArchive)) {
        "Restoring previous files because scheduled update output did not pass validation." | Tee-Object -FilePath $LogFile -Append
        Restore-ProtectedFiles
        exit 4
    }

    "Finished scheduled news update at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') with exit code $exitCode" | Tee-Object -FilePath $LogFile -Append
    exit $exitCode
}
finally {
    Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
}
