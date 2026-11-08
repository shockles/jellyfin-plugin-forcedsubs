<# : -------- batch part: runs the PowerShell below --------
@echo off
title Forced Subtitles setup
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([ScriptBlock]::Create([IO.File]::ReadAllText('%~f0')))"
set RC=%ERRORLEVEL%
if not defined FORCEDSUBS_NOPAUSE pause
exit /b %RC%
#>
# -------- PowerShell part --------
# One-click setup for the Jellyfin Forced Subtitles service on Windows.
# Double-click this file. It is safe to run again: it updates an existing install.
#
#   1. Installs Python 3.12 and ffmpeg if they are missing (winget, or direct download)
#   2. Checks for an NVIDIA GPU
#   3. Downloads the latest service release from GitHub into C:\ForcedSubs\service
#   4. Creates the Python environment and downloads the models (~6.5 GB the first time)
#   5. Makes the service start automatically when you log in, and starts it now
#   6. Checks that it answers on http://127.0.0.1:8796
#
# Optional environment variables:
#   FORCEDSUBS_DIR          install folder (default C:\ForcedSubs)
#   FORCEDSUBS_TRANSLATION  nllb-3.3B (default), nllb-1.3B, or none (English only)
#   FORCEDSUBS_NOAUTOSTART  set to 1 to skip starting with Windows

# PowerShell cmdlets stop on error; external programs (python, winget, ...) are judged by
# their exit codes instead. With a global 'Stop', Windows PowerShell 5.1 turns harmless
# stderr output (download progress bars) into fatal errors when output is redirected.
$ErrorActionPreference = 'Continue'
$PSDefaultParameterValues = @{ '*:ErrorAction' = 'Stop' }
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo        = 'shockles/jellyfin-plugin-forcedsubs'
$Root        = if ($env:FORCEDSUBS_DIR) { $env:FORCEDSUBS_DIR } else { 'C:\ForcedSubs' }
$ServiceDir  = Join-Path $Root 'service'
$Translation = if ($env:FORCEDSUBS_TRANSLATION) { $env:FORCEDSUBS_TRANSLATION } else { 'nllb-3.3B' }
$AutoStart   = -not $env:FORCEDSUBS_NOAUTOSTART
$TaskName    = 'Jellyfin Forced Subtitles'
$HealthUrl   = 'http://127.0.0.1:8796/health'
$Steps       = 6

function Step($n, $text) { Write-Host ''; Write-Host "[$n/$Steps] $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    !!  $text" -ForegroundColor Yellow }
function Info($text) { Write-Host "        $text" }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Have-Winget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }

function Winget-Install($id, $extra) {
    $wingetArgs = @('install', '-e', '--id', $id, '--silent', '--accept-package-agreements', '--accept-source-agreements') + $extra
    & winget @wingetArgs | Out-Host
    Refresh-Path
}

# Returns @(exe, args...) for a working Python 3.10-3.13, or $null.
function Find-Python {
    $candidates = @()
    foreach ($v in '3.12', '3.13', '3.11', '3.10') { $candidates += ,@('py', "-$v") }
    $candidates += ,@('python')
    $candidates += ,@("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match '^3\.(1[0-3])$') { return ,$c }
        } catch { }
    }
    return $null
}

function Download($url, $file) {
    Info "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
}

try {
    Write-Host '=== Forced Subtitles for Jellyfin: setup ===' -ForegroundColor White
    Info "Install folder: $Root"
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Refresh-Path

    # ------------------------------------------------------------------ 1. Python + ffmpeg
    Step 1 'Python and ffmpeg'
    $py = Find-Python
    if ($py) {
        Ok "Python found ($($py -join ' '))"
    } else {
        Info 'Installing Python 3.12...'
        if (Have-Winget) {
            Winget-Install 'Python.Python.3.12' @('--scope', 'user')
        }
        $py = Find-Python
        if (-not $py) {
            $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
            Download 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' $installer
            $p = Start-Process $installer -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
                'Include_launcher=1', 'InstallLauncherAllUsers=0', 'Include_test=0' -Wait -PassThru
            if ($p.ExitCode -ne 0) { throw "Python installer failed (exit code $($p.ExitCode))" }
            Refresh-Path
            $py = Find-Python
        }
        if (-not $py) { throw 'Python was installed but cannot be found. Restart the PC and run this again.' }
        Ok "Python installed ($($py -join ' '))"
    }

    if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        Ok 'ffmpeg found'
    } else {
        Info 'Installing ffmpeg...'
        if (Have-Winget) { Winget-Install 'Gyan.FFmpeg' @() }
        if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
            $zip = Join-Path $env:TEMP 'ffmpeg-release-essentials.zip'
            Download 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $zip
            $dest = Join-Path $Root 'ffmpeg'
            if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
            Expand-Archive $zip -DestinationPath $dest -Force
            $bin = (Get-ChildItem $dest -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (($userPath -split ';') -notcontains $bin) {
                [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $bin), 'User')
            }
            Refresh-Path
        }
        if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw 'ffmpeg could not be installed.' }
        Ok 'ffmpeg installed'
    }

    # ------------------------------------------------------------------ 2. GPU
    Step 2 'NVIDIA GPU'
    $gpu = $null
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
    }
    if ($gpu) { Ok $gpu } else {
        Warn 'No NVIDIA GPU or driver found. Install the latest NVIDIA driver, or the model check below will fail.'
    }

    # ------------------------------------------------------------------ 3. Download the service
    Step 3 'Downloading the latest service release'
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ 'User-Agent' = 'ForcedSubs-Setup' }
    $asset = $release.assets | Where-Object { $_.name -like 'forced-subs-service_*.zip' } | Select-Object -First 1
    if (-not $asset) { throw "No service zip found in release $($release.tag_name)." }
    Info "Release $($release.tag_name)"

    # Stop a running copy so its files can be replaced.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*forced_subs.server*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $zip = Join-Path $env:TEMP $asset.name
    Download $asset.browser_download_url $zip
    $staging = Join-Path $env:TEMP ('forced-subs-' + [guid]::NewGuid().ToString('N'))
    Expand-Archive $zip -DestinationPath $staging -Force
    New-Item -ItemType Directory -Force -Path $ServiceDir | Out-Null
    # Replace the code but keep .venv (installed packages) and data (history, logs).
    Get-ChildItem $ServiceDir -Force | Where-Object { $_.Name -notin '.venv', 'data' } | Remove-Item -Recurse -Force
    Copy-Item (Join-Path $staging '*') $ServiceDir -Recurse -Force
    Remove-Item $staging -Recurse -Force
    Ok "Service files in $ServiceDir"

    # ------------------------------------------------------------------ 4. Python environment + models
    Step 4 'Installing packages and downloading models (the first run takes a while)'
    $venvPy = Join-Path $ServiceDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        $exe = $py[0]; $rest = @($py | Select-Object -Skip 1)
        & $exe @rest -m venv (Join-Path $ServiceDir '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ServiceDir 'install.ps1') -TranslationModel $Translation
    if ($LASTEXITCODE -ne 0) { throw 'install.ps1 failed (see the messages above).' }
    Ok 'Packages and models ready'

    # ------------------------------------------------------------------ 5. Start (and start with Windows)
    Step 5 'Starting the service'
    # Launcher that reloads PATH first, so ffmpeg is found even right after it was installed.
    $launcher = Join-Path $Root 'run-service.ps1'
    @(
        '# Started at logon by the Forced Subtitles setup. Reloads PATH, then runs the service.',
        '$env:Path = [Environment]::GetEnvironmentVariable(''Path'', ''Machine'') + '';'' + [Environment]::GetEnvironmentVariable(''Path'', ''User'')',
        "& '$ServiceDir\start.ps1'"
    ) | Set-Content -Path $launcher -Encoding UTF8
    $psArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""

    $started = $false
    if ($AutoStart) {
        try {
            $action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs -WorkingDirectory $ServiceDir
            $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
                -Description 'Local Whisper service for the Jellyfin Forced Subtitles plugin' -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            $started = $true
            Ok "Scheduled task '$TaskName' registered (starts when you log in)"
        } catch {
            # No permission for scheduled tasks: use the Startup folder instead.
            $startup = [Environment]::GetFolderPath('Startup')
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut((Join-Path $startup 'Jellyfin Forced Subtitles.lnk'))
            $lnk.TargetPath = 'powershell.exe'
            $lnk.Arguments = $psArgs
            $lnk.WorkingDirectory = $ServiceDir
            $lnk.WindowStyle = 7
            $lnk.Save()
            Ok 'Added to the Startup folder (starts when you log in)'
        }
    }
    if (-not $started) {
        Start-Process powershell.exe -ArgumentList $psArgs -WorkingDirectory $ServiceDir -WindowStyle Hidden
    }

    # ------------------------------------------------------------------ 6. Health check
    Step 6 'Checking the service'
    $health = $null
    for ($i = 0; $i -lt 60 -and -not $health; $i++) {
        Start-Sleep -Seconds 2
        try { $health = Invoke-RestMethod $HealthUrl -TimeoutSec 3 } catch { }
    }
    if (-not $health) {
        throw "The service did not answer on $HealthUrl. See $ServiceDir\data\service.log"
    }
    Ok "Service v$($health.version) is running ($($health.cuda_devices) GPU)"

    Write-Host ''
    Write-Host '=== All done ===' -ForegroundColor Green
    Write-Host 'In Jellyfin: Dashboard > Plugins > Repositories > +, add'
    Write-Host "  https://raw.githubusercontent.com/$Repo/main/manifest.json" -ForegroundColor White
    Write-Host 'then install "Forced Subtitles (Whisper)" from the Catalog and restart Jellyfin.'
    Write-Host 'The plugin page should say "Service connected".'
    exit 0
} catch {
    Write-Host ''
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
