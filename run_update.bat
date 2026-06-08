@echo off
setlocal
cd /d "%~dp0"

set "BUNDLED_PY=C:\Users\GRAM_\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED_PY%" (
    echo Running news update with bundled Python...
    "%BUNDLED_PY%" "%~dp0main.py" %*
) else (
    echo Running news update with system Python...
    py "%~dp0main.py" %*
)
