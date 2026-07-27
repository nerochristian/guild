@echo off
setlocal

cd /d "%~dp0"

if exist "%~dp0.env" goto skip_env_check
echo Missing .env file in "%~dp0"
echo Please make sure the .env file is inside the bot folder!
pause
exit /b 1

:skip_env_check
where python >nul 2>nul
if errorlevel 1 goto missing_python
goto start_bot

:missing_python
echo Python was not found in PATH.
echo Install Python or add it to PATH, then run this file again.
pause
exit /b 1

:start_bot
echo Starting Soul Discord Bot...
python bot.py
set "exit_code=%errorlevel%"

if "%exit_code%"=="0" goto end_script
echo.
echo Bot crashed or exited with code %exit_code%.
pause

:end_script
exit /b %exit_code%
