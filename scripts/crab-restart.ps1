# crab-restart.ps1: from inside a session started by crab.ps1, ask the wrapper
# to update and resume this session. From Git Bash:
#   powershell -ExecutionPolicy Bypass -File ~/code/bad-ass-mcp/scripts/crab-restart.ps1
param([switch]$DryRun)

$name = $env:CRAB_NAME
$dir = if ($env:CRAB_HOME) { $env:CRAB_HOME } else { Join-Path $HOME ".claude\crab" }
if (-not $name) {
    Write-Error "crab-restart: CRAB_NAME is not set; this session was not started by the crab wrapper"
    exit 2
}

# Walk up from this process to the claude that owns it. ($pid is PowerShell's
# read-only $PID, names being case-insensitive, so the cursor is $walk.) The
# executable name decides: claude.exe itself, or node/bun running claude.
$walk = $PID
$proc = $null
while ($walk -gt 0) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $walk" -ErrorAction SilentlyContinue
    if (-not $p) { break }
    $exe = [System.IO.Path]::GetFileName($p.Name).ToLower()
    $args = if ($p.CommandLine) { $p.CommandLine } else { "" }
    if ($exe -in @("claude.exe", "claude") -or
        ($exe -in @("node.exe", "bun.exe") -and $args -match '[\\/ ]claude(\.js|\.cmd)?("| |$)')) {
        $proc = $p; break
    }
    $walk = $p.ParentProcessId
}
if (-not $proc) {
    Write-Error "crab-restart: no claude process above this shell"
    exit 1
}

if ($DryRun) {
    Write-Output "would write $dir\$name.restart and end claude pid $($proc.ProcessId): $($proc.CommandLine)"
    exit 0
}

New-Item -ItemType File -Force -Path (Join-Path $dir "$name.restart") | Out-Null
Write-Output "crab-restart: $name is going down for an update; the wrapper resumes this session"
# Stop-Process is TerminateProcess either way on Windows (no graceful exit);
# claude's transcript is written per message, so --resume survives it.
Start-Process -WindowStyle Hidden powershell -ArgumentList @(
    "-NoProfile", "-Command",
    "Start-Sleep 1; Stop-Process -Id $($proc.ProcessId) -ErrorAction SilentlyContinue"
)
