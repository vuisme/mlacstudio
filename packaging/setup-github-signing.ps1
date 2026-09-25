[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$PrivateKeyPath,
  [string]$PublicKeyPath=(Join-Path $PSScriptRoot 'keys\mlac-update-public.json'),
  [string]$GitHubSecretName='MLAC_UPDATE_SIGNING_KEY_B64',
  [string]$Repository='vuisme/mlacstudio',
  [switch]$UploadSecret
)
$ErrorActionPreference='Stop'
if (-not (Test-Path -LiteralPath $PrivateKeyPath)) {
  & (Join-Path $PSScriptRoot 'new-signing-key.ps1') -PrivateKeyPath $PrivateKeyPath -PublicKeyPath $PublicKeyPath
}
$private=[IO.File]::ReadAllBytes((Resolve-Path $PrivateKeyPath))
$encoded=[Convert]::ToBase64String($private)
Write-Host "Public key: $PublicKeyPath"
Write-Host "GitHub secret: $GitHubSecretName"
if ($UploadSecret) {
  if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh is required to upload the secret.' }
  $encoded | & gh secret set $GitHubSecretName --repo $Repository
  if ($LASTEXITCODE) { throw 'GitHub secret upload failed.' }
  Write-Host "Uploaded protected signing key to $Repository."
} else {
  Write-Host 'Secret was not uploaded. Pass -UploadSecret after GitHub authentication.'
}
