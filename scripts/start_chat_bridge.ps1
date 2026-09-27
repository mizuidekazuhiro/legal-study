param(
    [string]$PdfPath,

    [string]$Subject = "criminal",

    [string]$BridgeRoot,

    [string]$ObsidianInbox,

    [string]$Config = (Join-Path $env:USERPROFILE ".legal-study\service\config.json")
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Launcher = Join-Path $RepoRoot "scripts\Start-LegalStudyWatcher.ps1"
foreach ($File in @($Launcher, $Config)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Required file does not exist: $File"
    }
}
$settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($pair in @(
    @("PdfPath", $PdfPath, [string]$settings.pdf),
    @("Subject", $Subject, [string]$settings.subject),
    @("BridgeRoot", $BridgeRoot, [string]$settings.bridge_root),
    @("ObsidianInbox", $ObsidianInbox, [string]$settings.obsidian_inbox)
)) {
    if (-not [string]::IsNullOrWhiteSpace($pair[1]) -and $pair[1] -ne $pair[2]) {
        throw "$($pair[0]) does not match the supervised service config"
    }
}
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Launcher + '" -Config "' + $Config + '"'
$Supervisor = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments `
    -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru

[pscustomobject]@{
    SupervisorPid = $Supervisor.Id
    Config = (Resolve-Path -LiteralPath $Config).Path
    PdfPath = [string]$settings.pdf
    Subject = [string]$settings.subject
    BridgeRoot = [string]$settings.bridge_root
    ObsidianInbox = [string]$settings.obsidian_inbox
    NotionEnabled = $false
}
