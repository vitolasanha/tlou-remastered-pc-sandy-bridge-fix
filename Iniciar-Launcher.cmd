@echo off
rem Opens the launcher without TLOU Launcher.exe (nothing to compile): same as that program does.
rem In a release folder it uses the bundled python; otherwise the pythonw on PATH.
cd /d "%~dp0"
if exist "python\pythonw.exe" (
    start "" "python\pythonw.exe" "launcher\tlou_launcher.pyw" %*
) else (
    start "" pythonw "launcher\tlou_launcher.pyw" %*
)
