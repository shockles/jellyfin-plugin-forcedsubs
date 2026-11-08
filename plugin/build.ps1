# Builds the plugin and packages it as dist\ForcedSubs_0.2.0.0.zip
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
dotnet build Jellyfin.Plugin.ForcedSubs\Jellyfin.Plugin.ForcedSubs.csproj -c Release
$out = "Jellyfin.Plugin.ForcedSubs\bin\Release\net10.0"
$stage = "dist\Forced Subtitles_0.2.0.0"
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item "$out\Jellyfin.Plugin.ForcedSubs.dll" $stage -Force
Compress-Archive -Path "$stage\*" -DestinationPath "dist\ForcedSubs_0.2.0.0.zip" -Force
Write-Host "Packaged dist\ForcedSubs_0.2.0.0.zip"
