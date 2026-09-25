[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("draft", "promote", "rollback")][string]$Action,
    [Parameter(Mandatory = $true)][string]$Version,
    [ValidateSet("stable", "beta", "dev")][string]$Channel = "stable",
    [Parameter(Mandatory = $true)][string]$Approve,
    [string]$AssetsDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\mlac-release"),
    [string]$RollbackVersion
)

$ErrorActionPreference = "Stop"
if ($Approve -ne "I APPROVE MLAC RELEASE $Action") {
    throw "Human approval phrase did not match: I APPROVE MLAC RELEASE $Action"
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "GitHub CLI (gh) is required." }
$repo = "vuisme/mlacstudio"
$tag = "v$Version"
if ($Action -eq "draft") {
    $assets = Get-ChildItem -LiteralPath $AssetsDirectory -File | Where-Object Name -Like "MLAC-Studio-*"
    if (-not $assets) { throw "No MLAC Studio release assets were found." }
    & gh release create $tag --repo $repo --draft --title "MLAC Studio $Version" --notes-file (Join-Path $AssetsDirectory "CHANGELOG.md") @($assets.FullName)
} elseif ($Action -eq "promote") {
    & gh release edit $tag --repo $repo --draft=false --prerelease:($Channel -ne "stable") --latest:($Channel -eq "stable")
} else {
    if (-not $RollbackVersion) { throw "-RollbackVersion is required for rollback." }
    & gh release edit "v$RollbackVersion" --repo $repo --latest
}
if ($LASTEXITCODE -ne 0) { throw "GitHub release operation failed." }
