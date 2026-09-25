[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [string]$TemplatePath = (Join-Path $PSScriptRoot "release-manifest.example.json"),
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "config.json"),
    [string]$RuntimeDirectory,
    [string]$LocalFilesPath,
    [Parameter(Mandatory = $true)]
    [string]$UrlMapPath,
    [string]$ReleaseVersion
)

$ErrorActionPreference = "Stop"

function Read-JsonObject([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

$manifest = Read-JsonObject $TemplatePath "Manifest template"
$config = Read-JsonObject $ConfigPath "Application config"
$urlMap = Read-JsonObject $UrlMapPath "Artifact URL map"
$localFiles = if ($LocalFilesPath) {
    Read-JsonObject $LocalFilesPath "Local artifact map"
} else {
    [pscustomobject]@{}
}

if ($ReleaseVersion) {
    $manifest.release_version = $ReleaseVersion
}

foreach ($artifact in $manifest.artifacts) {
    $artifactId = [string]$artifact.id
    $sourcePath = $null
    if (($artifact.delivery -eq "bundled") -and $RuntimeDirectory) {
        $runtimeRelativePath = ([string]$artifact.path).Replace('/', '\')
        $sourcePath = Join-Path $RuntimeDirectory $runtimeRelativePath
    }
    if ((-not $sourcePath) -and ($artifact.PSObject.Properties.Name -contains "source_config_key")) {
        $configKey = [string]$artifact.source_config_key
        if ($config.PSObject.Properties.Name -contains $configKey) {
            $sourcePath = [string]$config.$configKey
        }
    }
    if (($localFiles.PSObject.Properties.Name -contains $artifactId) -and
        -not (($artifact.delivery -eq "bundled") -and $RuntimeDirectory)) {
        $sourcePath = [string]$localFiles.$artifactId
    }
    if ($sourcePath -and (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        $source = Get-Item -LiteralPath $sourcePath
        $artifact.size = [long]$source.Length
        $artifact.sha256 = (Get-FileHash -LiteralPath $source.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    } elseif ([string]$artifact.delivery -ne "download" -or [long]$artifact.size -le 0 -or
              -not ([string]$artifact.sha256 -match '^[0-9a-fA-F]{64}$')) {
        throw "No installed source file or pinned size/SHA-256 was provided for artifact '$artifactId'."
    }

    if ([string]$artifact.delivery -eq "download") {
        if (-not ($urlMap.PSObject.Properties.Name -contains $artifactId)) {
            throw "The URL map has no pinned HTTPS URL for '$artifactId'."
        }
        $url = [string]$urlMap.$artifactId
        if (-not $url.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "The URL for '$artifactId' must use HTTPS."
        }
        $artifact.url = $url
    } else {
        $artifact.url = ""
    }
    $artifact.PSObject.Properties.Remove("source_config_key")
}

$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$json = $manifest | ConvertTo-Json -Depth 20
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($resolvedOutput, $json + [Environment]::NewLine, $utf8NoBom)
Write-Host "Wrote release manifest: $resolvedOutput"
