$ErrorActionPreference = 'Stop'
$libraryRoot = Split-Path $PSScriptRoot -Parent
$state = Get-Content -LiteralPath (Join-Path $libraryRoot 'runtime\service.json') -Raw | ConvertFrom-Json
Invoke-RestMethod -Uri "$($state.baseUrl)/health" -TimeoutSec 5 | ConvertTo-Json
