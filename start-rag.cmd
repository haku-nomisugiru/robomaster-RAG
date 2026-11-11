@echo off
setlocal
pushd "%~dp0" || exit /b 1
python -B tools\restore_catalog.py || (
    popd
    exit /b 1
)
python -B tools\http_service.py %*
set "rag_exit=%errorlevel%"
popd
exit /b %rag_exit%
