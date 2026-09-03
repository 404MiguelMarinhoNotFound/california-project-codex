@echo off
REM ============================================================
REM PROJECT CALIFORNIA - Run Script (cmd.exe)
REM ============================================================
REM Shorthand for `uv run python main.py`. Any arguments are passed
REM straight through, e.g. `run.cmd --test-mic`. Double-clicking it
REM from Explorer works too; on failure the window stays open so the
REM error is readable instead of flashing past.
setlocal
cd /d "%~dp0"

REM Explorer (and any other shell) spawns a console just for this script,
REM so its name shows up in the command line. A console we were typed into
REM has only cmd.exe there, and should not be held open.
echo %cmdcmdline% | find /i "%~nx0" >nul
if not errorlevel 1 set OWN_CONSOLE=1

where uv >nul 2>&1
if errorlevel 1 set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>&1
if errorlevel 1 (
    echo uv not found. Run setup.ps1 first.
    set EXITCODE=1
    goto :end
)

uv run python main.py %*
set EXITCODE=%ERRORLEVEL%

:end
if defined OWN_CONSOLE if not "%EXITCODE%"=="0" pause
exit /b %EXITCODE%
