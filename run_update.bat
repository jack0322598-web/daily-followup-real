@echo off
setlocal
cd /d "%~dp0"

set "SYSTEM_PY=C:\Users\GRAM_\AppData\Local\Programs\Python\Python314\python.exe"
set "BUNDLED_PY=C:\Users\GRAM_\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%SYSTEM_PY%" (
    echo Running news update with system Python...
    "%SYSTEM_PY%" "%~dp0main.py" %*
) else if exist "%BUNDLED_PY%" (
    echo Running news update with bundled Python...
    "%BUNDLED_PY%" "%~dp0main.py" %*
) else (
    echo Running news update with Python launcher...
    py "%~dp0main.py" %*
)
