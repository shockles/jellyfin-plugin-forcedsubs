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
    [string]$Model = "large-v3",
    # Also download the translation model used for subtitle languages other than English
    # (nllb-3.3B ~3.3 GB, nllb-1.3B ~1.4 GB). Use "none" to skip; it downloads on first use anyway.
    [string]$TranslationModel = "nllb-3.3B"
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
    # "python" may be the Microsoft Store shortcut, which isn't real Python: check it runs.
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -match '^3\.(1[0-3])$') { return $cmd.Source }
    }
    throw "Python 3.10-3.13 not found. Install it from https://www.python.org/downloads/ (or run Install-ForcedSubs.bat) and run this again."
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

if ($TranslationModel -ne "none") {
    Write-Host "Downloading the $TranslationModel translation model..."
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    & .\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; from forced_subs.translate import MODELS; snapshot_download(MODELS.get('$TranslationModel', '$TranslationModel')); print('Translation model OK')"
    if ($LASTEXITCODE -ne 0) { Write-Warning "Translation model download failed; it will be retried on first use." }
}

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
