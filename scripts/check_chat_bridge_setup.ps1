param(
    [string]$RepoRoot,
    [string]$PythonPath,
    [string]$HomePath,
    [string]$BridgeRoot,
    [string]$ObsidianInbox,
    [string]$ObsidianVault = "C:\Obsidian\Kazuhiro Mizuide"
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) { $RepoRoot = Join-Path $PSScriptRoot ".." }
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
if (-not $PythonPath) {
    $PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not $HomePath) {
    $HomePath = if ($env:LEGAL_STUDY_HOME) {
        $env:LEGAL_STUDY_HOME
    } else {
        Join-Path $env:USERPROFILE ".legal-study"
    }
}
if (-not $BridgeRoot) {
    $BridgeRoot = if ($env:LEGAL_STUDY_CHAT_BRIDGE_ROOT) {
        $env:LEGAL_STUDY_CHAT_BRIDGE_ROOT
    } else {
        "G:\マイドライブ\LegalStudy_ChatBridge"
    }
}
if (-not $ObsidianInbox) {
    $ObsidianInbox = if ($env:LEGAL_STUDY_OBSIDIAN_INBOX) {
        $env:LEGAL_STUDY_OBSIDIAN_INBOX
    } else {
        "G:\マイドライブ\Obsidian_Inbox"
    }
}

$script:AllReady = $true
function Report([string]$Label, [bool]$Success) {
    if ($Success) {
        Write-Output "${Label}: OK"
    } else {
        Write-Output "${Label}: NG"
        $script:AllReady = $false
    }
}

$repoExists = Test-Path -LiteralPath $RepoRoot -PathType Container
$pythonExists = Test-Path -LiteralPath $PythonPath -PathType Leaf
$homeExists = Test-Path -LiteralPath $HomePath -PathType Container
Report "Repo" $repoExists
Report "Python" $pythonExists
Report "LEGAL_STUDY_HOME" $homeExists
Report "State DB" (Test-Path -LiteralPath (Join-Path $HomePath "state.sqlite3") -PathType Leaf)

$branch = $null
$head = $null
if ($repoExists) {
    try {
        $branch = (& git -C $RepoRoot branch --show-current 2>$null | Out-String).Trim()
        $head = (& git -C $RepoRoot rev-parse HEAD 2>$null | Out-String).Trim()
    } catch {
        $branch = $null
        $head = $null
    }
}
Report "Branch" (-not [string]::IsNullOrWhiteSpace($branch))
if ($branch) { Write-Output "Branch name: $branch" }
if ($head -match '^[0-9a-f]{40}$') {
    Write-Output "HEAD: $head"
} else {
    Write-Output "HEAD: NG"
    $script:AllReady = $false
}

Report "Bridge Root" (Test-Path -LiteralPath $BridgeRoot -PathType Container)
foreach ($folder in @("00_pending", "10_approved", "20_commands", "30_receipts", "99_failed")) {
    Report $folder (Test-Path -LiteralPath (Join-Path $BridgeRoot $folder) -PathType Container)
}
Report "Obsidian Inbox" (Test-Path -LiteralPath $ObsidianInbox -PathType Container)
Report "Obsidian Vault" (Test-Path -LiteralPath $ObsidianVault -PathType Container)
Report "Startup Script" (Test-Path -LiteralPath (Join-Path $RepoRoot "scripts/start_chat_bridge.ps1") -PathType Leaf)

$doctorReady = $false
$ocrReady = $false
if ($pythonExists -and $homeExists) {
    $previousHome = $env:LEGAL_STUDY_HOME
    $previousBytecode = $env:PYTHONDONTWRITEBYTECODE
    try {
        $env:LEGAL_STUDY_HOME = $HomePath
        $env:PYTHONDONTWRITEBYTECODE = "1"
        $doctorOutput = (& $PythonPath -m legal_study doctor 2>&1 | Out-String)
        $doctorReady = ($LASTEXITCODE -eq 0)
        $ocrOutput = (& $PythonPath -m legal_study doctor --ocr 2>&1 | Out-String)
        $ocrReady = ($LASTEXITCODE -eq 0 -and $ocrOutput -match 'Offline PaddleOCR: READY')
    } catch {
        $doctorReady = $false
        $ocrReady = $false
    } finally {
        $env:LEGAL_STUDY_HOME = $previousHome
        $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
    }
}
Report "Doctor" $doctorReady
if ($ocrReady) {
    Write-Output "OCR: READY"
} else {
    Write-Output "OCR: NG"
    $script:AllReady = $false
}

if ($script:AllReady) {
    Write-Output "Overall: READY"
    exit 0
}
Write-Output "Overall: NG"
exit 1
