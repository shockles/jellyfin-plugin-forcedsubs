# Starts the subtitle service on http://127.0.0.1:8796
# Logs go to .\data\service.log
Set-Location $PSScriptRoot
# Reload PATH so a freshly installed ffmpeg is found even when started at logon.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
& .\.venv\Scripts\python.exe -W ignore::UserWarning -m forced_subs.server --host 127.0.0.1 --port 8796 --idle-unload 60 @args
