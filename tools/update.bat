@echo off
setlocal
cd /d "%~dp0"
python updater.py
set "HELLOFISH_UPDATE_EXIT=%ERRORLEVEL%"
echo.
if not "%HELLOFISH_UPDATE_EXIT%"=="0" echo Update failed. See the message above.
pause
exit /b %HELLOFISH_UPDATE_EXIT%
