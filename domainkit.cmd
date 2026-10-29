@echo off
rem domainkit launcher. Comments are ASCII on purpose because cmd.exe reads
rem batch files in the console code page.
rem
rem Usage:
rem   domainkit.cmd names livingtechnology.com
rem   domainkit.cmd gui
rem   domainkit.cmd plan example.com --create-distribution --origin example.s3-website-us-east-1.amazonaws.com

setlocal EnableExtensions

set "CODEX_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "PY="

rem Prefer a regular Python installation. The Codex runtime is only a fallback.
for %%V in (314 313 312 311 310 39) do (
    if not defined PY (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
            set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        )
    )
)
for %%V in (314 313 312 311 310 39) do (
    if not defined PY (
        if exist "%ProgramFiles%\Python%%V\python.exe" (
            set "PY=%ProgramFiles%\Python%%V\python.exe"
        )
    )
)
if not defined PY (
    for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
        if not defined PY set "PY=%%P"
    )
)
if not defined PY if exist "%CODEX_PY%" set "PY=%CODEX_PY%"
if not exist "%PY%" (
    echo No usable Python found. Install Python 3.9+ or edit the PY variable in this file.
    exit /b 1
)

if defined DOMAINKIT_TCL_LIBRARY set "TCL_LIBRARY=%DOMAINKIT_TCL_LIBRARY%"
if defined DOMAINKIT_TK_LIBRARY set "TK_LIBRARY=%DOMAINKIT_TK_LIBRARY%"

rem Make the domainkit package importable from any working directory.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PYTHONUTF8=1"

rem Double-clicking the launcher opens the GUI; any CLI argument keeps CLI mode.
if "%~1"=="" goto :run_gui
if /i "%~1"=="gui" goto :run_gui

"%PY%" -m domainkit %*
exit /b %ERRORLEVEL%

:run_gui
call :check_tk
if errorlevel 1 if not "%PY%"=="%CODEX_PY%" if exist "%CODEX_PY%" (
    set "PY=%CODEX_PY%"
    call :check_tk
)
if errorlevel 1 (
    echo GUI requires Python with Tcl/Tk support.
    echo Install official Python 3.9+ with the tcl/tk component, or set
    echo DOMAINKIT_TCL_LIBRARY and DOMAINKIT_TK_LIBRARY to compatible data directories.
    exit /b 1
)
chcp 65001 >nul
set "PYW="
for %%P in ("%PY%") do (
    if exist "%%~dpPpythonw.exe" set "PYW=%%~dpPpythonw.exe"
)
if defined PYW (
    start "" "%PYW%" -m domainkit.gui
) else (
    "%PY%" -m domainkit.gui
)

exit /b %ERRORLEVEL%

:check_tk
if defined DOMAINKIT_DEBUG_LAUNCHER (
    echo [domainkit] Python: "%PY%"
    echo [domainkit] TCL_LIBRARY: "%TCL_LIBRARY%"
    echo [domainkit] TK_LIBRARY: "%TK_LIBRARY%"
    "%PY%" -c "import tkinter; tkinter.Tcl()"
) else (
    "%PY%" -c "import tkinter; tkinter.Tcl()" >nul 2>&1
)
exit /b %ERRORLEVEL%
