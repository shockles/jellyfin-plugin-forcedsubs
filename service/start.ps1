# Starts the forced-subtitle service on http://127.0.0.1:8796
# Logs go to .\data\service.log
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
& .\.venv\Scripts\python.exe -m forced_subs.server --host 127.0.0.1 --port 8796 --idle-unload 60 @args
