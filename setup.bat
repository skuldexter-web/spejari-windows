@echo off
rem Runs setup.ps1 without requiring a PowerShell execution-policy change.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if errorlevel 1 pause
