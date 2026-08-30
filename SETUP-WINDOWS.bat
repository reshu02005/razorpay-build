@echo off
REM ---------------------------------------------------------------------------
REM  RecoverAI -- one-click setup for Windows.
REM
REM  Double-click this file. It installs everything, creates the demo database
REM  and trains the ML model. It pauses at the end so the window stays open long
REM  enough to read -- a double-clicked .bat that closes instantly on error is
REM  the worst possible first experience.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"

echo.
echo  ==================================================================
echo    RecoverAI - first-time setup
echo  ==================================================================
echo.
echo  This will:
echo    1. create a Python virtual environment in .venv
echo    2. install the backend dependencies
echo    3. install the frontend dependencies (npm)
echo    4. create the demo database
echo    5. train the recovery-propensity model
echo.
echo  It takes a few minutes on a first run.
echo.
pause

call "%~dp0dev.bat" demo
if %ERRORLEVEL% NEQ 0 goto :failed

echo.
echo  ==================================================================
echo    Setup finished. Now double-click START-WINDOWS.bat
echo  ==================================================================
echo.
pause
exit /b 0

:failed
echo.
echo  ==================================================================
echo    Setup did not complete. Read the messages above.
echo    Run  dev.bat doctor  for a diagnosis of what is missing.
echo  ==================================================================
echo.
pause
exit /b 1
