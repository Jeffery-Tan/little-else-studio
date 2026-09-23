param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [int]$Port = 4760,
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv/Scripts/python.exe'),
    [switch]$AllowNew,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$selectedRoot = [System.IO.Path]::GetFullPath($DataRoot)
# A long-running terminal can retain the PATH from before a WinGet install.
# Reconcile only the registered user directories that contain both media tools.
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
    -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    foreach ($entry in ($userPath -split [IO.Path]::PathSeparator)) {
        if (-not $entry) { continue }
        $directory = [Environment]::ExpandEnvironmentVariables($entry.Trim('"'))
        if ((Test-Path -LiteralPath (Join-Path $directory 'ffmpeg.exe') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $directory 'ffprobe.exe') -PathType Leaf)) {
            $env:Path = "$directory$([IO.Path]::PathSeparator)$env:Path"
            break
        }
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Studio Python is missing: $Python. Create a Python 3.12 venv and install requirements-runtime.txt."
}

& $Python -V *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Studio Python cannot start: $Python. Repair the Python 3.12 venv and retry."
}

Push-Location -LiteralPath $appRoot
try {
    $env:PYTHONIOENCODING = 'utf-8'
    $arguments = @('-m', 'studio.doctor', '--data-root', $selectedRoot, '--port', [string]$Port)
    if ($AllowNew) { $arguments += '--allow-new' }
    if (-not $CheckOnly) { $arguments += '--start' }
    & $Python @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
