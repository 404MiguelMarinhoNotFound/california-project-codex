# ============================================================
# PROJECT CALIFORNIA - Run Script (Windows)
# ============================================================
# Shorthand for `uv run python main.py`. Any arguments are passed
# straight through, e.g. `.\run.ps1 --test-mic`.
$ErrorActionPreference = "Stop"

Set-Location -Path $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    # uv installs here by default but is not always on PATH yet
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Run .\setup.ps1 first."
    exit 1
}

uv run python main.py @args
