[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CoreDirectory,
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
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$coreArchive = Join-Path $OutputDirectory "MLAC-Studio-core-$Version.zip"
& $Python (Join-Path $PSScriptRoot "release-tools.py") archive --source $CoreDirectory --output $coreArchive

$manifest = Join-Path $OutputDirectory "MLAC-Studio-$Channel.json"
$arguments = @(
    (Join-Path $PSScriptRoot "release-tools.py"), "manifest",
    "--version", $Version, "--channel", $Channel, "--published-at", $PublishedAt,
    "--changelog", $Changelog,
    "--component", "core=core=$coreArchive",
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
