param(
    [Parameter(Mandatory = $true)]
    [string]$PdfPath,

    [string]$Subject = "criminal",

    [string]$BridgeRoot = "G:\マイドライブ\LegalStudy_ChatBridge",

    [string]$ObsidianInbox = "G:\マイドライブ\Obsidian_Inbox",

    [switch]$EnableNotion
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path (Split-Path $RepoRoot -Parent) ".venv\Scripts\python.exe"

foreach ($PathToCheck in @($Python, $PdfPath, $BridgeRoot, $ObsidianInbox)) {
    if (-not (Test-Path $PathToCheck)) {
        throw "Required path does not exist: $PathToCheck"
    }
}

$StudyArgs = @(
    "-m", "legal_study",
    "watch-study",
    $PdfPath,
    "--subject", $Subject,
    "--bridge-root", $BridgeRoot
)

$BridgeArgs = @(
    "-m", "legal_study",
    "watch-chat-bridge",
    "--bridge-root", $BridgeRoot,
    "--obsidian-inbox", $ObsidianInbox
)

if ($EnableNotion) {
    $BridgeArgs += "--enable-notion"
}

$StudyProcess = Start-Process -FilePath $Python -ArgumentList $StudyArgs -WorkingDirectory $RepoRoot -PassThru
$BridgeProcess = Start-Process -FilePath $Python -ArgumentList $BridgeArgs -WorkingDirectory $RepoRoot -PassThru

[pscustomobject]@{
    StudyWatcherPid = $StudyProcess.Id
    BridgeWatcherPid = $BridgeProcess.Id
    PdfPath = (Resolve-Path $PdfPath).Path
    Subject = $Subject
    BridgeRoot = (Resolve-Path $BridgeRoot).Path
    ObsidianInbox = (Resolve-Path $ObsidianInbox).Path
    NotionEnabled = [bool]$EnableNotion
}
