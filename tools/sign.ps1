# Create (once) a self-signed code-signing certificate and sign dist\ClassRollCall.exe.
# ASCII only.
#
# The certificate is a TEST certificate: it proves the file was not modified after
# signing, but it is NOT trusted by Windows until ClassRollCall.cer is imported into
# "Trusted Root Certification Authorities" + "Trusted Publishers" on the target PC.
param(
    [string]$PfxPassword = 'rollcall-test',
    [switch]$NoTimestamp
)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot | Split-Path -Parent
$exe = Join-Path $root 'dist\ClassRollCall.exe'
if (-not (Test-Path $exe)) { throw "missing $exe" }

$subject = 'CN=WhiteShadow Studio, O=WhiteShadow Studio, C=CN'

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -eq $subject } |
        Sort-Object NotAfter -Descending | Select-Object -First 1

if (-not $cert) {
    Write-Host 'creating self-signed code signing certificate...'
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $subject `
        -KeyUsage DigitalSignature `
        -KeyExportPolicy Exportable `
        -NotAfter (Get-Date).AddYears(5) `
        -CertStoreLocation Cert:\CurrentUser\My
    if (-not $cert) { throw 'New-SelfSignedCertificate failed' }
}

Write-Host ("cert: {0}  thumbprint {1}" -f $cert.Subject, $cert.Thumbprint)

# public part -> ships with the app
Export-Certificate -Cert $cert -FilePath (Join-Path $root 'dist\ClassRollCall.cer') -Force | Out-Null

# private part -> stays local, never shipped
$pfx = Join-Path $root '_tools\ClassRollCall.pfx'
$sec = ConvertTo-SecureString -String $PfxPassword -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $sec -Force | Out-Null

Write-Host 'signing...'
$args = @{
    FilePath = $exe
    Certificate = $cert
    HashAlgorithm = 'SHA256'
}
if (-not $NoTimestamp) { $args.TimestampServer = 'http://timestamp.digicert.com' }
$sig = Set-AuthenticodeSignature @args
if (-not $sig -or $sig.Status -ne 'Valid') {
    Write-Host ("first attempt status: {0}" -f $sig.Status)
    if ($args.ContainsKey('TimestampServer')) {
        Write-Host 'retry without timestamp...'
        $args.Remove('TimestampServer')
        $sig = Set-AuthenticodeSignature @args
    }
}

$check = Get-AuthenticodeSignature $exe
Write-Host ("status : {0}" -f $check.Status)
Write-Host ("signer : {0}" -f $check.SignerCertificate.Subject)
Write-Host ("sha256 : {0}" -f (Get-FileHash $exe -Algorithm SHA256).Hash)
