@echo off
REM ---------------------------------------------------------------------------
REM  RecoverAI -- one-click start for Windows.
REM
REM  Runs the API and the console together. Close this window or press Ctrl+C
REM  to stop both.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"

echo.
echo  ==================================================================
echo    Starting RecoverAI
echo      API      http://127.0.0.1:8000   (docs at /docs)
echo      Console  http://localhost:3000
echo    Press Ctrl+C to stop both servers.
echo  ==================================================================
echo.

call "%~dp0dev.bat" start
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  Startup failed. Run  dev.bat doctor  to see what is missing.
    echo.
    pause
)
exit /b %ERRORLEVEL%
