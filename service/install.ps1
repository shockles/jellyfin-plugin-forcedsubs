# Sets up the forced-subtitle service on this PC.
#   1. Creates a Python virtual environment in .venv
#   2. Installs faster-whisper + CUDA libraries
#   3. Downloads the Whisper large-v3 model (~3 GB) so the first job doesn't wait
#   4. Optionally registers it to start automatically at logon (-AutoStart)
#
# Run from PowerShell in this folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -AutoStart

param(
    [switch]$AutoStart,
    [string]$Model = "large-v3"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Python {
    foreach ($v in "3.13", "3.12", "3.11", "3.10") {
        try {
            $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch { }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python 3.10-3.13 not found. Install it from https://www.python.org/downloads/ and run this again."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg is not on PATH. Install it (for example: winget install Gyan.FFmpeg) or the service can't read movies."
}
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Warning "nvidia-smi not found. Make sure the NVIDIA driver is installed."
}

if (-not (Test-Path .venv\Scripts\python.exe)) {
    $py = Find-Python
    Write-Host "Creating virtual environment with $py"
    & $py -m venv .venv
}

Write-Host "Installing packages (this can take a few minutes)..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

Write-Host "Downloading and testing the $Model model on the GPU..."
& .\.venv\Scripts\python.exe -c "import forced_subs.cuda_setup; from faster_whisper import WhisperModel; WhisperModel('$Model', device='cuda', compute_type='int8_float16'); print('Model OK on GPU')"
if ($LASTEXITCODE -ne 0) { throw "Model test failed. See the error above." }

if ($AutoStart) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\start.ps1`"" `
        -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName "Jellyfin Forced Subtitles" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Local Whisper service for the Jellyfin Forced Subtitles plugin" -Force | Out-Null
    Start-ScheduledTask -TaskName "Jellyfin Forced Subtitles"
    Write-Host "Registered and started scheduled task 'Jellyfin Forced Subtitles' (runs at logon)."
} else {
    Write-Host "Done. Start the service with: .\start.ps1"
}
