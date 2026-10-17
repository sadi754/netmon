@echo off
rem Builds NetMon as a portable folder: dist\NetMon\NetMon.exe and its files,
rem plus dist\NetMon-<version>-win64.zip of that folder, ready to share, and -
rem when Inno Setup 6 is installed - the installer dist\NetMon-Setup-<version>.exe.
rem Needs the Python environment that run.bat sets up (run it once first).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [build] Run run.bat once first - it sets up the Python environment.
    exit /b 1
)
echo [build] Installing the build tools ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt -r requirements-build.txt || goto failed
".venv\Scripts\python.exe" tools\prepare_build.py || goto failed
echo [build] Building NetMon.exe (takes a minute or two) ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build\pyinstaller tools\netmon.spec || goto failed
".venv\Scripts\python.exe" tools\package_zip.py || goto failed

set "ISCC="
for %%d in ("%LOCALAPPDATA%\Programs\Inno Setup 6" "%ProgramFiles(x86)%\Inno Setup 6" "%ProgramFiles%\Inno Setup 6") do (
    if exist "%%~d\ISCC.exe" set "ISCC=%%~d\ISCC.exe"
)
if not defined ISCC (
    echo [build] Inno Setup 6 not found - skipping the installer ^(winget install JRSoftware.InnoSetup^).
    goto done
)
set /p VERSION=<build\version.txt
echo [build] Building the installer ...
"%ISCC%" /Q /DAppVersion=%VERSION% installer\netmon.iss || goto failed
echo [build] dist\NetMon-Setup-%VERSION%.exe

:done
echo [build] Done: dist\NetMon\NetMon.exe
exit /b 0

:failed
echo [build] Failed - see the messages above.
exit /b 1
