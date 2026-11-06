@echo off
rem Shows which instruction sets this CPU has and which ones the port will emulate in software.
cd /d "%~dp0"
if exist "python\python.exe" (
    "python\python.exe" gow3_pc\scripts\cpu_features.py
) else (
    python gow3_pc\scripts\cpu_features.py
)
pause
