param(
    [Parameter(Mandatory = $true)][string]$BusinessRoot,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [int]$Port = 4760,
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv/Scripts/python.exe'),
    [switch]$AllowNew,
    [switch]$CheckOnly,
    [switch]$Foreground,
    [switch]$NoReload
)
$ErrorActionPreference = 'Stop'
if ($BusinessRoot -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') { throw 'BusinessRoot must be an absolute Windows path.' }
$studioBusinessPath = [IO.Path]::GetFullPath($BusinessRoot)
if (-not (Test-Path -LiteralPath (Join-Path $studioBusinessPath 'tracks/us/creative/learning/library.json') -PathType Leaf)) {
    throw 'BusinessRoot must contain the existing US learning library.'
}
$studioPreviousBusinessRoot = $env:STUDIO_BUSINESS_ROOT
try {
    $env:STUDIO_BUSINESS_ROOT = $studioBusinessPath
    & (Join-Path $PSScriptRoot '../apps/us-video-studio/scripts/start.ps1') -DataRoot $DataRoot -Port $Port -Python $Python -AllowNew:$AllowNew -CheckOnly:$CheckOnly -Foreground:$Foreground -NoReload:$NoReload
} finally {
    $env:STUDIO_BUSINESS_ROOT = $studioPreviousBusinessRoot
}
