@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "ANONYMIZED_TELEMETRY=False"
"_runtime\python\python.exe" -B tools\http_service.py %*
set "rag_exit=%errorlevel%"
popd
if not "%rag_exit%"=="0" (
    echo.
    echo The service could not start. Check the error above.
    echo If port 8765 is occupied, run start-rag.cmd --port 8766
    if not defined RAG_NO_PAUSE pause
)
exit /b %rag_exit%
