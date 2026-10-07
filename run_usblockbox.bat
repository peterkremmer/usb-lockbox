@echo off
setlocal EnableExtensions
title USB Lockbox
cd /d "%~dp0"
set "CHECK=%~dp0tools\preflight.py"

REM Starts USB Lockbox. First it makes sure a suitable Python and the required packages are present;
REM if something is missing, it says what to do and keeps this window open.
REM Erasing drives needs administrator rights; the app itself asks for them at start (UAC prompt).

REM 1. Find a Python that passes the checks: supported version, 64-bit, installed for all users.
set "PYCMD="
for %%V in (3.13 3.14 3.12 3.11 3.10) do (
  if not defined PYCMD (
    py -%%V "%CHECK%" --python-only >nul 2>nul && set "PYCMD=py -%%V"
  )
)
if not defined PYCMD (
  python "%CHECK%" --python-only >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD goto :no_good_python

REM 2. Check the required packages and offer to install or update them.
%PYCMD% "%CHECK%"
if errorlevel 1 goto :setup_incomplete

REM 3. Start the app.
%PYCMD% -m usblockbox %*
if errorlevel 1 goto :app_failed
endlocal
exit /b 0

:no_good_python
set "ANYPY="
py -3 -c "import sys" >nul 2>nul && set "ANYPY=py -3"
if not defined ANYPY python -c "import sys" >nul 2>nul && set "ANYPY=python"
if defined ANYPY goto :explain_python
echo.
echo  USB Lockbox needs Python, and none was found on this computer.
echo.
echo  How to install Python for ALL USERS:
echo    1. Open https://www.python.org/downloads/windows/ and download the
echo       "Windows installer (64-bit)" for Python 3.13 or 3.14.
echo    2. Run it. On the first screen tick "Add python.exe to PATH", then click
echo       "Customize installation".
echo    3. Keep the default options and click Next. On the "Advanced Options" page tick
echo       "Install Python for all users" - the wording may differ slightly - then Install.
echo       Windows will ask you to approve administrator access.
echo    4. When it finishes, double-click run_usblockbox.bat again. No restart is needed.
goto :wait_and_exit

:explain_python
%ANYPY% "%CHECK%"
goto :wait_and_exit

:setup_incomplete
echo.
echo  USB Lockbox is not ready to start yet. Follow the instructions above,
echo  then double-click run_usblockbox.bat again.
goto :wait_and_exit

:app_failed
set "RC=%ERRORLEVEL%"
echo.
echo  USB Lockbox stopped with an error (code %RC%). Read the messages above.
goto :wait_and_exit

:wait_and_exit
echo.
pause
endlocal
exit /b 1
