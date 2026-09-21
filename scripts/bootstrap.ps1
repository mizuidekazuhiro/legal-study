$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -U pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Write-Host ""
Write-Host "Core local environment is ready."
Write-Host "Run:"
Write-Host "  .\.venv\Scripts\legal-study.exe init"
Write-Host "  .\.venv\Scripts\legal-study.exe doctor"
Write-Host ""
Write-Host "Optional OCR:"
Write-Host "  .\.venv\Scripts\python.exe -m pip install -e \".[ocr]\""
Write-Host "  # Then install the Paddle runtime appropriate for this PC."
