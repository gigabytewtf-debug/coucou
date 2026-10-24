@echo off
chcp 65001 >nul
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Sagban bot.lnk'); $s.TargetPath='%~dp0start_hidden.vbs'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo Bot endi kompyuter yoqilganda o'zi ishga tushadi (Startup papkasiga yorliq qo'yildi).
timeout /t 5 >nul
