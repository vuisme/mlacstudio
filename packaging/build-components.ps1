[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CoreDirectory,
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
    [string]$PublicKeyPath = (Join-Path $PSScriptRoot "keys\mlac-update-public.json"),
    [string]$Python = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$Version = "0.3.0",
    [ValidateSet("stable", "beta", "dev")][string]$Channel = "stable",
    [Parameter(Mandatory = $true)][string]$PublishedAt,
    [string]$Changelog = "",
    [string]$DataMigration,
    [switch]$SecurityMandatory,
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\mlac-release")
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$staging = Join-Path $root "dist\component-staging"
$common = Join-Path $staging "common-runtime"
$nvidia = Join-Path $staging "nvidia-runtime"
if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
New-Item -ItemType Directory -Path $common,$nvidia,$OutputDirectory -Force | Out-Null

$gpuPattern = '(?i)(cuda|cublas|cudnn|nvrtc|curand|cusparse|nvjpeg)'
Get-ChildItem -LiteralPath $RuntimeDirectory -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring((Resolve-Path -LiteralPath $RuntimeDirectory).Path.Length).TrimStart('\')
    $targetRoot = if ($_.Name -match $gpuPattern) { $nvidia } else { $common }
    $target = Join-Path $targetRoot $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
}

$coreArchive = Join-Path $OutputDirectory "MLAC-Studio-core-$Version.zip"
$commonArchive = Join-Path $OutputDirectory "MLAC-Studio-common-runtime-$Version.zip"
$nvidiaArchive = Join-Path $OutputDirectory "MLAC-Studio-nvidia-runtime-$Version.zip"
& $Python (Join-Path $PSScriptRoot "release-tools.py") archive --source $CoreDirectory --output $coreArchive
& $Python (Join-Path $PSScriptRoot "release-tools.py") archive --source $common --output $commonArchive
& $Python (Join-Path $PSScriptRoot "release-tools.py") archive --source $nvidia --output $nvidiaArchive

$manifest = Join-Path $OutputDirectory "MLAC-Studio-$Channel.json"
$arguments = @(
    (Join-Path $PSScriptRoot "release-tools.py"), "manifest",
    "--version", $Version, "--channel", $Channel, "--published-at", $PublishedAt,
    "--changelog", $Changelog,
    "--component", "core=core=$coreArchive",
    "--component", "common-runtime=common-runtime=$commonArchive",
    "--component", "nvidia-runtime=nvidia-runtime=$nvidiaArchive",
    "--private-key", $PrivateKeyPath,
    "--public-key", $PublicKeyPath,
    "--key-id", ((Get-Content -LiteralPath $PublicKeyPath -Raw | ConvertFrom-Json).key_id),
    "--output", $manifest
)
if ($DataMigration) { $arguments += @("--data-migration", $DataMigration) }
if ($SecurityMandatory) { $arguments += "--security-mandatory" }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "Component manifest build failed." }
Write-Host "Wrote deterministic component assets and signed $Channel metadata to $OutputDirectory"
