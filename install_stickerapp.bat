@echo off
rem One-time setup for StickerApp. Double-click this file.
rem   1. Finds Python 3, or installs it (with winget, which comes with Windows 10/11)
rem   2. Creates the .venv folder and installs what StickerApp needs
setlocal
title StickerApp installer
cd /d "%~dp0"

echo.
echo ==============================
echo   StickerApp installer
echo ==============================
echo.

set "PYEXE="

rem --- 1. Is Python 3 already installed? ---
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto havepython

python -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto havepython

rem --- 2. Not found: install it ---
echo Python was not found on this PC. Installing it now (this can take a few minutes)...
echo.
where winget >nul 2>&1
if errorlevel 1 goto nowinget
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
set PYEXE="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist %PYEXE% goto pyfail
echo.
echo Python installed.

:havepython
echo Using Python: %PYEXE%
echo.
echo Setting up StickerApp (a minute or two, lots of text is normal)...
echo.
%PYEXE% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo ==============================
echo   All done!
echo   To start StickerApp, double-click run_stickerapp.bat
echo ==============================
echo.
pause
exit /b 0

:nowinget
echo This PC cannot install Python automatically.
echo Please install Python from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" on the first screen), then double-click this file again.
goto end

:pyfail
echo The automatic Python install did not finish.
echo Please install Python from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" on the first screen), then double-click this file again.
goto end

:fail
echo.
echo Something went wrong during setup. Scroll up to see the error message.
echo If you need help, take a photo of this window and open an issue on GitHub.

:end
echo.
pause
exit /b 1
