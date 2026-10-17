@echo off
rem NetMon launcher: prepares the Python environment on first run, then starts the app.
rem   run.bat              desktop app (tray icon + window)
rem   run.bat --minimized  start in the tray only
rem   run.bat --console    live speeds in this terminal
rem   run.bat --list       list network adapters
rem   run.bat --debug      desktop app, with log messages in this terminal
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [NetMon] First run: creating a private Python environment in .venv ...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo [NetMon] Could not create .venv - is Python 3.10 or newer installed?
        pause
        exit /b 1
    )
)

rem Install packages only when requirements.txt has changed since the last install.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo [NetMon] Installing Python packages ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo [NetMon] Package installation failed - see the messages above.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

rem Terminal modes run here; the desktop app starts detached (no console window).
for %%A in (--console --list --debug -h --help) do if /i "%~1"=="%%A" goto terminal
start "" ".venv\Scripts\pythonw.exe" -m netmon %*
exit /b 0

:terminal
".venv\Scripts\python.exe" -m netmon %*
