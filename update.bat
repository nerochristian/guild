@echo off
setlocal
cd /d "%~dp0" || (
    echo Auto-update setup failed: could not open the repository folder.
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1 || (
    echo Auto-update setup failed: this folder is not a Git repository.
    exit /b 1
)

set "WATCHER=%~dp0auto_update_watcher.ps1"
set "LAUNCHER=%~dp0auto_update_launcher.vbs"
set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
set "RUN_VALUE=Guild Auto Update"

if not exist "%WATCHER%" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/nerochristian/guild/guild/auto_update_watcher.ps1' -OutFile '%WATCHER%'" >nul 2>&1
)
if not exist "%LAUNCHER%" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/nerochristian/guild/guild/auto_update_launcher.vbs' -OutFile '%LAUNCHER%'" >nul 2>&1
)
if not exist "%WATCHER%" goto :missing_watcher
if not exist "%LAUNCHER%" goto :missing_watcher

echo Installing automatic update watcher...
reg add "%RUN_KEY%" /v "%RUN_VALUE%" /t REG_SZ /d "wscript.exe \"%LAUNCHER%\"" /f >nul
if errorlevel 1 (
    echo Could not register startup. Starting the watcher for this session instead.
) else (
    echo Startup registration complete.
)

wscript.exe "%LAUNCHER%"

echo Auto-update is running in the background.
echo It will commit and push changes after 20 seconds without new edits.
echo It will launch again automatically when you sign in.
exit /b 0

:missing_watcher
echo Auto-update setup failed: watcher files are missing.
exit /b 1
