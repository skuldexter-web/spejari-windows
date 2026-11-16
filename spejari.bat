@echo off
rem Launch Spejari for Windows.
rem   spejari.bat           choose CLI or GUI at start-up
rem   spejari.bat --cli     terminal dashboard (use Windows Terminal)
rem   spejari.bat --gui     desktop window
rem   spejari.bat --simulate   try it without hardware
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
) else (
    where py >nul 2>nul && (py -3 main.py %*) || (python main.py %*)
)
if errorlevel 1 if "%~1"=="" pause
endlocal
