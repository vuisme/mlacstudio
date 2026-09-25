[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
    [string]$PublicKeyPath = (Join-Path $PSScriptRoot "keys\mlac-update-public.json"),
    [string]$KeyId = "mlac-release-2026-01"
)

$ErrorActionPreference = "Stop"
if ((Test-Path -LiteralPath $PrivateKeyPath) -or (Test-Path -LiteralPath $PublicKeyPath)) {
    throw "Refusing to overwrite an existing signing key. Rotate keys through an explicitly reviewed change."
}
$rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider 3072
$parameters = $rsa.ExportParameters($false)
$public = [ordered]@{
    algorithm = "rsa-sha256"
    key_id = $KeyId
    modulus = [Convert]::ToBase64String($parameters.Modulus)
    exponent = [Convert]::ToBase64String($parameters.Exponent)
}
if ($PSCmdlet.ShouldProcess($PrivateKeyPath, "Create private MLAC Studio release signing key")) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $PrivateKeyPath) -Force | Out-Null
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($PrivateKeyPath), $rsa.ToXmlString($true), (New-Object Text.UTF8Encoding($false)))
    New-Item -ItemType Directory -Path (Split-Path -Parent $PublicKeyPath) -Force | Out-Null
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($PublicKeyPath), (($public | ConvertTo-Json) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
}
