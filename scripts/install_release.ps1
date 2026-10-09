# Install an extracted release on native Windows without a Git checkout.
[CmdletBinding()]
param(
    [string]$From,
    [string]$ConfigDir
)
$ErrorActionPreference = 'Stop'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE)." }
}
$uv = (Get-Command uv -ErrorAction Stop).Source
if (-not $From) {
    $wheels = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter 'acp_gateway-*.whl')
    if ($wheels.Count -ne 1) { throw 'Expected one release wheel; specify -From explicitly.' }
    $From = $wheels[0].FullName
}
$binDir = (& $uv tool dir --bin).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate uv tool executables.' }
$entry = Join-Path $binDir 'acpgw.exe'
$constraints = Join-Path $PSScriptRoot 'requirements.lock.txt'
if (Test-Path -LiteralPath $entry) {
    $arguments = @()
    if ($ConfigDir) {
        $arguments += @('--config', (Join-Path $ConfigDir 'config.yaml'), '--env-file', (Join-Path $ConfigDir '.env'))
    }
    $arguments += @('update', '--from', $From)
    if (Test-Path -LiteralPath $constraints) { $arguments += @('--constraints', $constraints) }
    Invoke-Checked $entry $arguments
} else {
    $arguments = @('tool', 'install', '--python', '3.12', $From)
    if (Test-Path -LiteralPath $constraints) { $arguments += @('--constraints', $constraints) }
    Invoke-Checked $uv $arguments
}
$setup = @('setup')
if ($ConfigDir) { $setup += @('--config-dir', $ConfigDir) }
Invoke-Checked $entry $setup
Write-Output "Installed: $entry"
Write-Output 'Run uv tool update-shell if the tool directory is not on PATH.'
