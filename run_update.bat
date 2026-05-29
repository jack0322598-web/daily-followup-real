@echo off
setlocal
cd /d "%~dp0"

set "BUNDLED_PY=C:\Users\GRAM_\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED_PY%" (
    "%BUNDLED_PY%" "%~dp0main.py" %*
) else (
    py "%~dp0main.py" %*
)
