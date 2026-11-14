@echo off
setlocal
pushd "%~dp0" || exit /b 1
if exist "_runtime\python\python.exe" (
    "_runtime\python\python.exe" -B tools\cad_acquisition.py %*
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -B tools\cad_acquisition.py %*
) else (
    python -B tools\cad_acquisition.py %*
)
set "rag_exit=%errorlevel%"
popd
exit /b %rag_exit%
