@echo off
setlocal

set SCRIPT_DIR=%~dp0
set ROOT_DIR=%SCRIPT_DIR%..
set OUT_DIR=%ROOT_DIR%\data\SYS1\LOADLIB

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

cl /nologo /O2 "%SCRIPT_DIR%iefbr14.c" /Fe:"%OUT_DIR%\IEFBR14.exe"
if errorlevel 1 exit /b 1

copy /Y "%SCRIPT_DIR%cobcomp.py" "%OUT_DIR%\cobcomp.py" >nul
(
echo @echo off
echo set SCRIPT_DIR=%%~dp0
echo py "%%SCRIPT_DIR%%cobcomp.py" %%*
) > "%OUT_DIR%\COBCOMP.cmd"

echo Built %OUT_DIR%\IEFBR14.exe
echo Installed %OUT_DIR%\COBCOMP.cmd
exit /b 0
