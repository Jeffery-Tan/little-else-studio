param(
    [string]$BusinessRoot,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [int]$Port = 4760,
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv/Scripts/python.exe'),
    [switch]$AllowNew,
    [switch]$CheckOnly,
    [switch]$Foreground,
    [switch]$NoReload
)
$ErrorActionPreference = 'Stop'
$studioBusinessPath = $null
if ($BusinessRoot) {
    if ($BusinessRoot -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') { throw 'BusinessRoot must be an absolute Windows path.' }
    $studioBusinessPath = [IO.Path]::GetFullPath($BusinessRoot)
    if (-not (Test-Path -LiteralPath $studioBusinessPath -PathType Container)) {
        throw 'BusinessRoot must be an existing business directory; it does not supply Studio knowledge.'
    }
}
$studioPreviousBusinessRoot = $env:STUDIO_BUSINESS_ROOT
try {
    $env:STUDIO_BUSINESS_ROOT = $studioBusinessPath
    & (Join-Path $PSScriptRoot '../apps/us-video-studio/scripts/start.ps1') -DataRoot $DataRoot -Port $Port -Python $Python -AllowNew:$AllowNew -CheckOnly:$CheckOnly -Foreground:$Foreground -NoReload:$NoReload
} finally {
    $env:STUDIO_BUSINESS_ROOT = $studioPreviousBusinessRoot
}
