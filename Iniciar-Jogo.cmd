@echo off
rem Starts the game without the launcher (settings saved by the launcher are used). Log: analysis\tlou\boot.log
rem TLOU_ISA_REWRITE=auto (default) | force | off  -  software replacement of BMI1/LZCNT/MOVBE/F16C.
cd /d "%~dp0"
if exist "python\python.exe" (
    "python\python.exe" tools\probe_tlou.py --boot --seconds 0 %*
) else (
    python tools\probe_tlou.py --boot --seconds 0 %*
)
pause
