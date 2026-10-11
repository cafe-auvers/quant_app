@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_replay.ps1" -OpenBrowser
if errorlevel 1 pause
