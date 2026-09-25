[CmdletBinding()]
param(
    [string]$Version = "0.3.0",
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\mlac-release")
)

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "bootstrap\MLACStudioBootstrap.cs"
$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw "The .NET Framework C# compiler was not found: $compiler"
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = Join-Path $OutputDirectory "MLAC-Studio-Setup-$Version.exe"
& $compiler /nologo /target:winexe /optimize+ "/out:$output" `
    /reference:System.dll /reference:System.Core.dll /reference:System.Windows.Forms.dll `
    /reference:System.Drawing.dll /reference:System.Web.Extensions.dll `
    /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll $source
if ($LASTEXITCODE -ne 0) {
    throw "Bootstrap compilation failed."
}
Write-Host "Wrote bootstrap installer: $output ($((Get-Item -LiteralPath $output).Length) bytes)"
