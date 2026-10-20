@echo off
rem domainkit launcher. No install needed.
rem
rem This machine has no usable Python on PATH, so the launcher points at the
rem Python 3.12 runtime that ships with Codex. Comments are ASCII on purpose:
rem cmd.exe reads batch files in the console code page, and UTF-8 text here
rem would be mangled.
rem
rem Usage:
rem   domainkit.cmd names livingtechnology.com
rem   domainkit.cmd gui
rem   domainkit.cmd plan example.com --create-distribution --origin example.s3-website-us-east-1.amazonaws.com

setlocal

set "PY_EXE="
set "PY_ARGS="

if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PY_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PY_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_EXE for /f "delims=" %%I in ('where py 2^>nul') do if not defined PY_EXE set "PY_EXE=py" & set "PY_ARGS=-3"
if not defined PY_EXE for /f "delims=" %%I in ('where python 2^>nul') do if not defined PY_EXE set "PY_EXE=python"
if not defined PY_EXE (
    echo No usable Python found.
    echo Install Python 3.9+ or edit this file to point at your interpreter.
    pause
    exit /b 1
)

rem Make the domainkit package importable from any working directory.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if "%~1"=="" goto :run_gui
if /i "%~1"=="gui" goto :run_gui

:run_cli
"%PY_EXE%" %PY_ARGS% -m domainkit %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo domainkit exited with code %RC%.
    pause
)
exit /b %RC%

:run_gui
"%PY_EXE%" %PY_ARGS% -m domainkit.gui
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo domainkit GUI exited with code %RC%.
    pause
)
exit /b %RC%
