param(
    [Parameter(Mandatory = $true)]
    [string]$PdfPath,

    [string]$Subject = "criminal",

    [string]$BridgeRoot,

    [string]$ObsidianInbox
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if ([string]::IsNullOrWhiteSpace($BridgeRoot)) {
    $BridgeRoot = if ($env:LEGAL_STUDY_CHAT_BRIDGE_ROOT) {
        $env:LEGAL_STUDY_CHAT_BRIDGE_ROOT
    } else {
        "G:\マイドライブ\LegalStudy_ChatBridge"
    }
}
if ([string]::IsNullOrWhiteSpace($ObsidianInbox)) {
    $ObsidianInbox = if ($env:LEGAL_STUDY_OBSIDIAN_INBOX) {
        $env:LEGAL_STUDY_OBSIDIAN_INBOX
    } else {
        "G:\マイドライブ\Obsidian_Inbox"
    }
}

foreach ($File in @($Python, $PdfPath)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Required file does not exist: $File"
    }
}
foreach ($Folder in @(
    $BridgeRoot,
    (Join-Path $BridgeRoot "00_pending"),
    (Join-Path $BridgeRoot "10_approved"),
    (Join-Path $BridgeRoot "20_commands"),
    (Join-Path $BridgeRoot "30_receipts"),
    (Join-Path $BridgeRoot "99_failed"),
    $ObsidianInbox
)) {
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) {
        throw "Required folder does not exist: $Folder"
    }
}

# Start-Process joins ArgumentList elements; quote every value so paths with spaces survive.
function Join-ProcessArguments([string[]]$Values) {
    return (($Values | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join ' ')
}

$StudyArgs = Join-ProcessArguments @(
    "-m", "legal_study",
    "watch-study",
    $PdfPath,
    "--subject", $Subject,
    "--bridge-root", $BridgeRoot
)

$BridgeArgs = Join-ProcessArguments @(
    "-m", "legal_study",
    "watch-chat-bridge",
    "--bridge-root", $BridgeRoot,
    "--obsidian-inbox", $ObsidianInbox
)

$StudyProcess = $null
try {
    $StudyProcess = Start-Process -FilePath $Python -ArgumentList $StudyArgs `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    $BridgeProcess = Start-Process -FilePath $Python -ArgumentList $BridgeArgs `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
} catch {
    if ($null -ne $StudyProcess -and -not $StudyProcess.HasExited) {
        Stop-Process -Id $StudyProcess.Id
    }
    throw
}

[pscustomobject]@{
    StudyWatcherPid = $StudyProcess.Id
    BridgeWatcherPid = $BridgeProcess.Id
    PdfPath = (Resolve-Path -LiteralPath $PdfPath).Path
    Subject = $Subject
    BridgeRoot = (Resolve-Path -LiteralPath $BridgeRoot).Path
    ObsidianInbox = (Resolve-Path -LiteralPath $ObsidianInbox).Path
    NotionEnabled = $false
}
