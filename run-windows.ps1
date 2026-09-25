[CmdletBinding()]
param(
    [int]$Port = 8730,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Set-Location -LiteralPath $RepoRoot

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Local environment is missing. Run 'uv sync' in this folder first."
}

& $VenvPython -c "import PIL, pystray" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Pillow or pystray is missing from .venv. Run 'uv sync' in this folder first."
}

$launcherArgs = @(
    (Join-Path $RepoRoot "packaging\launcher.py"),
    "--config", (Join-Path $RepoRoot "config.json"),
    "--port", $Port
)
if ($NoBrowser) {
    $launcherArgs += "--no-browser"
}
& $VenvPython @launcherArgs
