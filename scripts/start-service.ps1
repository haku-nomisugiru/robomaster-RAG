param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$libraryRoot = Split-Path $PSScriptRoot -Parent
$runtimeDir = Join-Path $libraryRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw 'Port already in use.' }
$pythonPath = Join-Path $libraryRoot '.venv\Scripts\python.exe'
$servicePath = Join-Path $libraryRoot 'tools\http_service.py'
$serviceArgs = @('-B', ('"' + $servicePath + '"'), '--port', "$Port")
$process = Start-Process -FilePath $pythonPath -ArgumentList $serviceArgs -WorkingDirectory $libraryRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'stdout.log') -RedirectStandardError (Join-Path $runtimeDir 'stderr.log') -PassThru
@{pid=$process.Id; port=$Port; startedUtc=$process.StartTime.ToUniversalTime().ToString('o'); servicePath=$servicePath; baseUrl="http://127.0.0.1:$Port"} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeDir 'service.json') -Encoding utf8
Write-Output "Started PID $($process.Id). Run status-service.ps1 to verify readiness."
