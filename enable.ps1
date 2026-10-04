$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path 'C:\ProgramData\Auto Episodes Downloader' | Out-Null
Set-Content -LiteralPath 'C:\ProgramData\Auto Episodes Downloader\mpvnet-default-associations.xml' -Encoding UTF8 -Value '<?xml version="1.0" encoding="UTF-8"?>
<DefaultAssociations>
  <Association Identifier=".mp4" ProgId="mpvnet.mp4" ApplicationName="mpv.net" />
  <Association Identifier=".mkv" ProgId="mpvnet.mkv" ApplicationName="mpv.net" />
</DefaultAssociations>
'
$key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System'
if (-not (Test-Path $key)) { New-Item -Path $key | Out-Null }
Set-ItemProperty -Path $key -Name 'DefaultAssociationsConfiguration' -Value 'C:\ProgramData\Auto Episodes Downloader\mpvnet-default-associations.xml'
exit 0