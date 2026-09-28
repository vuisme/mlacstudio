[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$UrlMapPath,
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
    [Parameter(Mandatory = $true)][string]$PublishedAt,
    [string]$LocalFilesPath,
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "config.json"),
    [string]$Python = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$Version,
    [ValidateSet("stable", "beta", "dev")][string]$Channel = "stable",
    [string]$Changelog = "",
    [string]$DataMigration,
    [switch]$SecurityMandatory,
    [switch]$SkipPortable
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
foreach ($path in @($Python, $PrivateKeyPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file not found: $path" }
}
if (-not $Version) {
    $projectText = Get-Content -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Raw
    $match = [regex]::Match($projectText, '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw "Could not read the project version." }
    $Version = $match.Groups[1].Value
}

$releaseRoot = Join-Path $projectRoot "dist\mlac-release"
if (Test-Path -LiteralPath $releaseRoot) {
    Remove-Item -LiteralPath $releaseRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
$modelManifest = Join-Path $releaseRoot "release-manifest.json"
$manifestArgs = @{
    OutputPath = $modelManifest; ConfigPath = $ConfigPath
    UrlMapPath = $UrlMapPath; ReleaseVersion = $Version
}
if ($LocalFilesPath) { $manifestArgs.LocalFilesPath = $LocalFilesPath }
& (Join-Path $PSScriptRoot "build-release-manifest.ps1") @manifestArgs
& $Python (Join-Path $PSScriptRoot "validate-release-sources.py") --manifest $modelManifest --hydrate
if ($LASTEXITCODE -ne 0) { throw "Generated model manifest metadata hydration failed." }
& $Python (Join-Path $PSScriptRoot "model-manager.py") validate --manifest $modelManifest
if ($LASTEXITCODE -ne 0) { throw "Generated model manifest failed validation." }
& $Python (Join-Path $PSScriptRoot "validate-release-sources.py") --manifest $modelManifest
if ($LASTEXITCODE -ne 0) { throw "Generated model manifest failed remote validation." }

$versionMetadata = Join-Path $releaseRoot "version.json"
@{ version = $Version } | ConvertTo-Json -Compress | Set-Content -LiteralPath $versionMetadata -Encoding utf8
$env:MLAC_MODEL_MANIFEST = (Resolve-Path -LiteralPath $modelManifest).Path
$env:MLAC_VERSION_METADATA = (Resolve-Path -LiteralPath $versionMetadata).Path
try {
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "mlac-studio.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller core build failed." }
} finally {
    Remove-Item Env:MLAC_MODEL_MANIFEST -ErrorAction SilentlyContinue
    Remove-Item Env:MLAC_VERSION_METADATA -ErrorAction SilentlyContinue
}

$bundle = Join-Path $projectRoot "dist\MLACStudio"
foreach ($required in @("MLACStudio.exe", "release-manifest.json", "model-manager.py", "updater.py")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundle $required) -PathType Leaf)) { throw "Core bundle is missing $required" }
}
if (Test-Path -LiteralPath (Join-Path $bundle "runtime")) { throw "Core component must not contain native inference runtimes." }

$componentArgs = @{
    CoreDirectory = $bundle; PrivateKeyPath = $PrivateKeyPath
    Python = $Python; Version = $Version; Channel = $Channel; PublishedAt = $PublishedAt
    Changelog = $Changelog; OutputDirectory = $releaseRoot
}
if ($DataMigration) { $componentArgs.DataMigration = $DataMigration }
if ($SecurityMandatory) { $componentArgs.SecurityMandatory = $true }
& (Join-Path $PSScriptRoot "build-components.ps1") @componentArgs
& (Join-Path $PSScriptRoot "build-bootstrap.ps1") -Version $Version -OutputDirectory $releaseRoot

if (-not $SkipPortable) {
    & (Join-Path $PSScriptRoot "build-portable.ps1") -BundleDirectory $bundle -Version $Version -OutputDirectory $releaseRoot
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "DEPLOYMENT.md") -Destination $releaseRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "QWEN_RESEARCH_LICENSE_NOTICE.txt") -Destination $releaseRoot -Force

$releaseFiles = Get-ChildItem -LiteralPath $releaseRoot -File | Where-Object Name -ne "SHA256SUMS.txt"
$sumLines = foreach ($file in ($releaseFiles | Sort-Object Name)) {
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($file.Name)"
}
$sumLines | Set-Content -LiteralPath (Join-Path $releaseRoot "SHA256SUMS.txt") -Encoding ascii
Write-Host "MLAC Studio release artifacts are in $releaseRoot"
