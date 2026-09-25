[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDirectory,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
$output = [System.IO.Path]::GetFullPath($OutputDirectory)

$weights = Get-ChildItem -LiteralPath $bundle -Recurse -File | Where-Object {
    $_.Extension.ToLowerInvariant() -in @(".gguf", ".safetensors", ".ckpt", ".pt", ".pth")
}
if ($weights) {
    throw "Portable bundle contains model weights: $($weights.FullName -join ', ')"
}
if (Test-Path -LiteralPath (Join-Path $bundle "models")) {
    throw "Portable bundle must not contain a models directory."
}

New-Item -ItemType Directory -Path $output -Force | Out-Null
$zipPath = Join-Path $output "MLAC-Studio-portable-$Version.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path $bundle -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Wrote portable archive: $zipPath"
