$ErrorActionPreference = 'Stop'
$libraryRoot = Split-Path $PSScriptRoot -Parent
$statePath = Join-Path $libraryRoot 'runtime\service.json'
if (!(Test-Path -LiteralPath $statePath)) { Write-Output 'No recorded service.'; exit 0 }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
if ($process) {
    $command = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.pid)"
    if ($process.StartTime.ToUniversalTime().ToString('o') -ne $state.startedUtc -or !$command.CommandLine.Contains($state.servicePath)) {
        throw 'Recorded PID belongs to another process; refusing to stop it.'
    }
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($state.pid)" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($state.servicePath) })
    foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $state.pid -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $statePath
Write-Output 'Service stopped.'
