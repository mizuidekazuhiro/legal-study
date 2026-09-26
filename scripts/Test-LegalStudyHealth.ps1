param([string]$OutputDirectory = 'C:\ProgramData\CodexProgramAudit\reports', [string]$UserRoot = 'C:\Users\kmizu')
$ErrorActionPreference = 'Stop'
# The SYSTEM audit never executes user-writable Python or follows paths from its heartbeat.
$repo = Join-Path $UserRoot 'legal-study'
$root = Join-Path $UserRoot '.legal-study\service'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$entry = Join-Path $repo 'src\legal_study\automation\watch_service.py'
$issues = [Collections.Generic.List[string]]::new()
$task = Get-ScheduledTask -TaskName 'LegalStudy-OCR-Watcher' -TaskPath '\' -ErrorAction SilentlyContinue
$info = if ($task) { $task | Get-ScheduledTaskInfo } else { $null }
if (!$task) { $issues.Add('OCR_TASK_MISSING') }
elseif ($task.State -eq 'Disabled') { $issues.Add('OCR_TASK_DISABLED') }
if (!(Test-Path -LiteralPath $entry -PathType Leaf)) { $issues.Add('ENTRYPOINT_MISSING') }
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { $issues.Add('VENV_PYTHON_MISSING') }
if (!(Test-Path -LiteralPath (Join-Path $repo 'scripts\Start-LegalStudyWatcher.ps1'))) { $issues.Add('LAUNCHER_MISSING') }
if (!(Test-Path -LiteralPath (Join-Path $UserRoot '.legal-study\models\paddleocr\model_manifest.json'))) { $issues.Add('OCR_MODEL_MANIFEST_MISSING') }
$state = $null
$path = Join-Path $root 'service-state.json'
if (Test-Path -LiteralPath $path) {
    if ((Get-Item -LiteralPath $path).Length -le 64KB) {
        for ($attempt=0; $attempt -lt 10; $attempt++) {
            try { $state = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json; break }
            catch { if($attempt -eq 9){$issues.Add('INVALID_HEARTBEAT')}else{Start-Sleep -Milliseconds 50} }
        }
    } else { $issues.Add('OVERSIZED_HEARTBEAT') }
}
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$interactive = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue).Count -gt 0
$processVerified = $false
if ($state -and $state.pid) {
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$state.pid) -ErrorAction SilentlyContinue
    $processVerified = $null -ne $process -and $process.CommandLine -match 'legal_study\.automation\.watch_service' -and $state.python -eq $python
    if ($interactive -and (!$processVerified -or $now - [double]$state.heartbeat_at -gt 180)) { $issues.Add('WATCHER_NOT_HEALTHY_WHILE_LOGGED_ON') }
    if ($state.status -eq 'failed') { $issues.Add('WATCHER_ABNORMAL_EXIT') }
    if ($state.last_error -and $now - [double]$state.last_error.at -lt 86400) { $issues.Add('RECENT_WORKER_ERROR') }
    if ($interactive -and !$state.pdf_available) { $issues.Add('PDF_SYNC_PATH_UNAVAILABLE_IN_USER_SESSION') }
} elseif ($interactive -and $task) { $issues.Add('HEARTBEAT_MISSING') }
if ($info -and $info.LastTaskResult -notin @(0,267009,267011) -and (!$state -or $state.status -ne 'stopped')) { $issues.Add('TASK_LAST_RESULT_NONZERO') }
$logErrors = 0; $logBytes = 0L
foreach ($name in @('supervisor.log','study.log','bridge.log','launcher.log')) {
    $path = Join-Path $root $name
    if (Test-Path -LiteralPath $path) {
        $file = Get-Item -LiteralPath $path; $logBytes += $file.Length
        if ($file.Length -gt 3MB) { $issues.Add('LOG_SIZE_OVER_LIMIT') }
        if ($file.LastWriteTime -gt (Get-Date).AddDays(-1)) {
            $logErrors += @(Get-Content -LiteralPath $path -Tail 100 | Where-Object {$_ -match '\bERROR\b|Traceback \(most recent call last\)|EXIT code=[1-9]'}).Count
        }
    }
}
if ($logErrors) { $issues.Add('RECENT_LOG_ERRORS') }
$related = @()
foreach ($name in @('ObsidianInboxRelay','Notion to Anki Sync','Anki Notion Revlog Sync')) {
    $other = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
    $otherInfo = if ($other) { $other | Get-ScheduledTaskInfo } else { $null }
    $normalResident = $other -and $other.State -eq 'Running' -and $otherInfo.LastTaskResult -in @(0,267009,2147946720)
    $related += [pscustomobject]@{Task=$name;State=if($other){[string]$other.State}else{'Missing'};LastResult=if($otherInfo){$otherInfo.LastTaskResult}else{$null};NormalResident=$normalResident}
    if (!$other -or $other.State -eq 'Disabled') { $issues.Add('RELATED_TASK_UNAVAILABLE: '+$name) }
    elseif (!$normalResident -and $otherInfo.LastTaskResult -notin @(0,267009,267011)) { $issues.Add('RELATED_TASK_FAILED: '+$name) }
}
$free = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($UserRoot)).AvailableFreeSpace
if ($free -lt 5GB) { $issues.Add('DISK_FREE_BELOW_5_GIB') }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$snapshot = Join-Path $OutputDirectory 'legal-study-health-latest.json'
$delta = $null
if ((Test-Path -LiteralPath $snapshot) -and (Get-Item -LiteralPath $snapshot).Length -le 64KB) {
    try {
        $old = Get-Content -LiteralPath $snapshot -Raw | ConvertFrom-Json
        $delta = [long]$old.FreeBytes - $free
        if ($delta -gt 5GB) { $issues.Add('VOLUME_FREE_SPACE_DECREASE_OVER_5_GIB') }
    } catch { $issues.Add('PREVIOUS_HEALTH_UNREADABLE') }
}
$result = [pscustomobject]@{
    CheckedAt=(Get-Date).ToString('o'); Status=if($issues.Count){'WARN'}else{'OK'}
    TaskRegistered=($null -ne $task); TaskEnabled=($task -and $task.State -ne 'Disabled')
    TaskState=if($task){[string]$task.State}else{'Missing'}; LastTaskResult=if($info){$info.LastTaskResult}else{$null}
    Entrypoint=$entry; Python=$python; ProcessVerified=$processVerified
    ServiceState=if($state){$state.status}else{'Unknown'}; RecentLogErrorLines=$logErrors
    CurrentLogBytes=$logBytes; FreeBytes=$free; FreeBytesDecrease=$delta
    RelatedTasks=$related
    StorageScope='Volume free-space delta and bounded logs; no recursive PDF/cache scan'
    Issues=@($issues); PdfReprocessed=$false; PythonExecuted=$false
}
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $snapshot -Encoding UTF8
$report = Join-Path $OutputDirectory 'legal-study-health-latest.md'
@('# LegalStudy OCR health check','',"Status: $($result.Status)","Task: $($result.TaskState)","Service: $($result.ServiceState)","Process verified: $processVerified",("Free disk GiB: {0:N2}" -f ($free/1GB)),"Issues: $($issues -join ', ')",'No PDF/OCR/cache regeneration; no user-owned Python executed by SYSTEM.') | Set-Content -LiteralPath $report -Encoding UTF8
[pscustomobject]@{Status=$result.Status;Issues=@($issues);SnapshotPath=$snapshot;ReportPath=$report}
