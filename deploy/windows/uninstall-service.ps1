#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string] $ServiceDir = (Join-Path $env:ProgramData 'wsi-patchkit-viewer')
)

$ErrorActionPreference = 'Stop'
$serviceName = 'wsi-patchkit-viewer'
$serviceId = 'wsiPatchkitViewer'
$wrapperPath = Join-Path $ServiceDir "$serviceName.exe"
if (-not (Test-Path -LiteralPath $wrapperPath)) {
    throw "WinSW service executable not found: $wrapperPath"
}

$service = Get-Service -Name $serviceId -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne 'Stopped') {
    Stop-Service -Name $serviceId
    $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
}

& $wrapperPath uninstall
if ($LASTEXITCODE -ne 0) {
    throw "WinSW uninstall failed with exit code $LASTEXITCODE"
}
Write-Host "Removed service registration. Configuration, logs, cache, and crop data remain in $ServiceDir."
