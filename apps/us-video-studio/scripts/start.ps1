param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [int]$Port = 4760,
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'LittleElse/Studio/runtime-venv/Scripts/python.exe'),
    [switch]$AllowNew,
    [switch]$CheckOnly,
    [switch]$Foreground,
    [switch]$NoReload
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
    $runtimeUrl = "http://127.0.0.1:$Port/api/runtime"
    $rootId = & $Python -c 'import hashlib,sys; from pathlib import Path; print(hashlib.sha256(str(Path(sys.argv[1]).resolve()).casefold().encode()).hexdigest()[:12])' $selectedRoot
    if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the selected Studio data root.' }
    $sourceIdsLines = & $Python -c 'import hashlib,sys; from studio.paths import PROJECT_ROOT,BUSINESS_ROOT; print(hashlib.sha256(str(PROJECT_ROOT).casefold().encode()).hexdigest()[:12]); print(hashlib.sha256(str(BUSINESS_ROOT).casefold().encode()).hexdigest()[:12] if BUSINESS_ROOT is not None else sys.argv[1])' not-configured
    if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the selected Studio code and business roots.' }
    $sourceIds = @{ source_root_id = $sourceIdsLines[0].Trim(); business_root_id = $sourceIdsLines[1].Trim() }
    $existing = $null
    try { $existing = Invoke-RestMethod -Uri $runtimeUrl -TimeoutSec 2 } catch {}
    if ($existing -and -not $CheckOnly) {
        if ($existing.data_root_id -ne $rootId.Trim()) {
            throw "Port $Port belongs to another data root. Connect to that service or choose another port."
        }
        if ($existing.source_root_id -ne $sourceIds.source_root_id -or $existing.business_root_id -ne $sourceIds.business_root_id) {
            throw 'This port uses another code or business root (or a legacy service). Wait for its jobs to finish, stop it normally, then start this Studio.'
        }
        if (-not $NoReload -and -not $existing.hot_reload.enabled) {
            throw "The current service uses the old launcher. Wait for all jobs to finish, stop that service once, then run start.ps1 again."
        }
        Write-Output "Studio is already running: http://127.0.0.1:$Port/ (PID $($existing.process_id), reload $($existing.hot_reload.enabled))"
        exit 0
    }
    $arguments = @('-m', 'studio.doctor', '--data-root', $selectedRoot, '--port', [string]$Port)
    if ($AllowNew) { $arguments += '--allow-new' }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($CheckOnly) { exit 0 }
    $arguments += '--start'
    if (-not $NoReload) { $arguments += '--reload' }
    if ($Foreground) {
        & $Python @arguments
        exit $LASTEXITCODE
    }
    # Paths are command-line arguments, never interpolated shell commands.
    $quotedArguments = foreach ($argument in $arguments) {
        if ($argument.Contains('"')) { throw 'A Studio startup argument contains an unsupported quote.' }
        '"' + ($argument -replace '(\\+)$', '$1$1') + '"'
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outLog = Join-Path $env:TEMP "studio-$Port-$stamp.stdout.log"
    $errLog = Join-Path $env:TEMP "studio-$Port-$stamp.stderr.log"
    $process = Start-Process -FilePath $Python -ArgumentList $quotedArguments -WorkingDirectory $appRoot `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $runtime = $null
        try { $runtime = Invoke-RestMethod -Uri $runtimeUrl -TimeoutSec 2 } catch {}
        if ($runtime -and $runtime.data_root_id -eq $rootId.Trim() -and
            ($NoReload -or $runtime.hot_reload.enabled) -and
            ($NoReload -or $runtime.hot_reload.supervisor_id -eq $process.Id -or
             # A Windows venv redirector may own a separate physical Python PID.
             (Get-CimInstance Win32_Process -Filter "ProcessId=$($runtime.hot_reload.supervisor_id)").ParentProcessId -eq $process.Id)) {
            Write-Output "Studio ready: http://127.0.0.1:$Port/ (worker PID $($runtime.process_id), reload $($runtime.hot_reload.enabled))"
            Write-Output "Logs: $outLog | $errLog"
            exit 0
        }
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    throw "Studio did not become ready. Startup PID $($process.Id). Inspect $outLog and $errLog; no other process was stopped."
}
finally {
    Pop-Location
}
