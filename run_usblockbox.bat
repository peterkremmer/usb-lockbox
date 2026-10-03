@echo off
REM Starts USB Lockbox. Erasing drives needs administrator rights; the app asks for them at start (UAC prompt).
cd /d "%~dp0"
where py >nul 2>nul && (py -m usblockbox %*) || (python -m usblockbox %*)
