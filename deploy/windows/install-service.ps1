#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string] $SlideDir = '',
    [string] $ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string] $ServiceDir = (Join-Path $env:ProgramData 'wsi-patchkit-viewer')
)

$ErrorActionPreference = 'Stop'
$serviceName = 'wsi-patchkit-viewer'
$serviceId = 'wsiPatchkitViewer'
$wrapperSource = Join-Path $ProjectDir 'WinSW-x64.exe'
$viewerExecutable = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$templatePath = Join-Path $PSScriptRoot 'wsi-patchkit-viewer.xml'

foreach ($path in @($wrapperSource, $viewerExecutable, $templatePath, $SlideDir)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required path does not exist: $path"
    }
}
$SlideDir = (Resolve-Path -LiteralPath $SlideDir).Path
$slideRoot = [System.IO.Path]::GetPathRoot($SlideDir)
if ($SlideDir.Length -gt $slideRoot.Length) {
    $SlideDir = $SlideDir.TrimEnd('\')
}

New-Item -ItemType Directory -Force -Path $ServiceDir, (Join-Path $ServiceDir 'logs'), (Join-Path $ServiceDir 'cache\kfb-index'), (Join-Path $ServiceDir 'data\crops'), (Join-Path $ServiceDir 'data\uploads') | Out-Null
$wrapperPath = Join-Path $ServiceDir "$serviceName.exe"
$configPath = Join-Path $ServiceDir "$serviceName.xml"
Copy-Item -LiteralPath $wrapperSource -Destination $wrapperPath -Force

function ConvertTo-XmlText([string] $Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}

$config = Get-Content -LiteralPath $templatePath -Raw
$config = $config.Replace('@VIEWER_EXE@', (ConvertTo-XmlText $viewerExecutable))
$config = $config.Replace('@PROJECT_DIR@', (ConvertTo-XmlText $ProjectDir))
$config = $config.Replace('@SLIDE_DIR@', (ConvertTo-XmlText $SlideDir))
Set-Content -LiteralPath $configPath -Value $config -Encoding UTF8

$null = [xml](Get-Content -LiteralPath $configPath -Raw)
& $wrapperPath install
if ($LASTEXITCODE -ne 0) {
    throw "WinSW install failed with exit code $LASTEXITCODE"
}

$service = Get-CimInstance Win32_Service -Filter "Name='$serviceId'"
if (-not $service) {
    throw "Windows service '$serviceId' was not found after installation"
}
foreach ($directory in @((Join-Path $ServiceDir 'logs'), (Join-Path $ServiceDir 'cache'), (Join-Path $ServiceDir 'data'))) {
    & icacls.exe $directory /grant "$($service.StartName):(OI)(CI)M" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant the service account write access to $directory"
    }
}

Write-Host "Installed '$serviceId'. Start it with: Start-Service $serviceId"
Write-Host "Open http://127.0.0.1:8000 after the service starts."
