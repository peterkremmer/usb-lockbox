@echo off
REM Starts the station. Real mode needs an elevated prompt (Run as administrator).
cd /d "%~dp0"
python -m usblockbox %*
