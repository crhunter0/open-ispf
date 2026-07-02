@echo off
setlocal

set SCRIPT_DIR=%~dp0
set ROOT_DIR=%SCRIPT_DIR%..
set OUT_DIR=%ROOT_DIR%\data\SYS1\LOADLIB

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

cl /nologo /O2 "%SCRIPT_DIR%iefbr14.c" /Fe:"%OUT_DIR%\IEFBR14.exe"
if errorlevel 1 exit /b 1

echo Built %OUT_DIR%\IEFBR14.exe
exit /b 0
