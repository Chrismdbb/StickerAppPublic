@echo off
rem Starts StickerApp with a visible window (handy for testing). Close the window to stop it.
rem   run_stickerapp.bat                  real printers
rem   run_stickerapp.bat --simulate 2     two fake printers, no hardware needed
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=py"
)
"%PY%" app\main.py --open %*
echo.
echo StickerApp stopped. Press any key to close this window.
pause >nul
