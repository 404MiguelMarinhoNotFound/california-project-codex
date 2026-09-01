@echo off
REM Lets `run` work from cmd.exe or a double-click. Delegates to run.ps1.
powershell -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
