$ErrorActionPreference = "Stop"

function Get-CompatiblePython {
    $candidates = @(
        @{ Command = "py"; Args = @("-3.13") },
        @{ Command = "py"; Args = @("-3.12") },
        @{ Command = "py"; Args = @("-3.11") },
        @{ Command = "python"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) {
            continue
        }

        $candidateArgs = @($candidate.Args)
        try {
            $versionText = & $candidate.Command @candidateArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $versionText) {
                continue
            }

            $parts = $versionText.Trim().Split(".")
            if ($parts.Count -lt 2) {
                continue
            }

            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    throw "Python 3.11 or newer was not found. Install Python 3.11+ and re-run this script."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $python = Get-CompatiblePython
    $pythonArgs = @($python.Args)

    Write-Host "Creating .venv with $($python.Command) $($pythonArgs -join ' ')"
    & $python.Command @pythonArgs -m venv .venv

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path ".venv\Scripts\python.exe")) {
        throw "Failed to create .venv."
    }
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
Write-Host '  .\.venv\Scripts\python.exe -m pip install -e ".[ocr]"'
Write-Host "  .\.venv\Scripts\legal-study.exe warmup-ocr  # run once while online"
Write-Host "  .\.venv\Scripts\legal-study.exe doctor --ocr"
