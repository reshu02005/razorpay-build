@echo off
REM ---------------------------------------------------------------------------
REM  RecoverAI -- Windows launcher.
REM
REM  Thin passthrough to dev.py so Windows users can type `dev setup` instead of
REM  `python dev.py setup`. All the real logic lives in dev.py, once, for every
REM  platform -- this file only solves "which Python executable is on PATH".
REM
REM    dev setup     dev seed      dev train
REM    dev start     dev backend   dev frontend
REM    dev test      dev doctor    dev demo
REM ---------------------------------------------------------------------------
setlocal

REM The Python launcher `py` is installed by the python.org installer and is the
REM most reliable way to find a real Python on Windows.
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYEXE=py -3"
    goto :run
)

REM `python` needs to be probed, not merely found. On a stock Windows 10/11 the
REM Microsoft Store app-execution alias sits on PATH at
REM %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe, so `where python` succeeds
REM on a machine with no Python at all. Running the stub prints an advert for the
REM Store and exits 9009. Actually executing it is the only reliable way to tell
REM a real interpreter from the alias -- and getting this wrong means the helpful
REM message below (the python.org link, and the reminder to tick "Add python.exe
REM to PATH") is never the thing the user sees.
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 goto :nopython

python -c "import sys" >nul 2>nul
if %ERRORLEVEL% NEQ 0 goto :nopython

set "PYEXE=python"
goto :run

:nopython
echo.
echo   [FAIL] Python was not found on your PATH.
echo.
echo   Install Python 3.10, 3.11, 3.12 or 3.13 from https://www.python.org/downloads/
echo   IMPORTANT: tick "Add python.exe to PATH" in the installer, then open a
echo   NEW terminal window and try again.
echo.
exit /b 1

:run
%PYEXE% "%~dp0dev.py" %*
exit /b %ERRORLEVEL%
