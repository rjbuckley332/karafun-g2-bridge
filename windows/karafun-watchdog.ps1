# karafun-watchdog.ps1 — health-check the three karaoke services every minute,
# restart whatever died. Runs as SYSTEM via the KaraFunWatchdog scheduled task.
$ErrorActionPreference = 'SilentlyContinue'
$logFile = Join-Path $PSScriptRoot 'watchdog.log'

function Test-Http($url) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Restart-Task($task, $why) {
    schtasks /run /tn $task | Out-Null
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') restarted $task ($why)" | Out-File -Append $logFile
}

if (-not (Test-Http 'http://127.0.0.1:8766/api/live')) { Restart-Task 'KaraFunBridge' 'bridge 8766 down' }
if (-not (Test-Http 'http://127.0.0.1:8768/position')) { Restart-Task 'KaraSingEar' 'ear 8768 down' }
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8769/health' -TimeoutSec 3
    if (-not $h.ok) { Restart-Task 'KaraFunControlHelper' 'helper bad answer' }
} catch { Restart-Task 'KaraFunControlHelper' 'helper 8769 down' }
