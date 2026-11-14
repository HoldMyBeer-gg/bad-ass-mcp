# crab.ps1: run a named Claude Code session that can restart itself up to date.
# The Windows form of scripts/crab; see docs/crab.md.
#
#   crab.ps1 [-Name rusty-win-crab] [claude args...]
param(
    [string]$Name = $(if ($env:CRAB_NAME) { $env:CRAB_NAME } else { "$env:COMPUTERNAME-crab".ToLower() }),
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ClaudeArgs = @()
)

$dir = if ($env:CRAB_HOME) { $env:CRAB_HOME } else { Join-Path $HOME ".claude\crab" }
$repo = if ($env:CRAB_REPO) { $env:CRAB_REPO } else { Join-Path $HOME "code\bad-ass-mcp" }
$py = if ($env:CRAB_PYTHON) { $env:CRAB_PYTHON } else { "python" }
$claude = if ($env:CRAB_CLAUDE) { $env:CRAB_CLAUDE } else { "claude" }
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$sessionFile = Join-Path $dir "$Name.session"
$flag = Join-Path $dir "$Name.restart"

$fresh = -not (Test-Path $sessionFile) -or -not (Get-Content $sessionFile -First 1)
if ($fresh) { [guid]::NewGuid().ToString() | Set-Content $sessionFile }
$session = (Get-Content $sessionFile -First 1).Trim()

function Update-Crab {
    if (Test-Path (Join-Path $repo ".git")) {
        git -C $repo pull -q --ff-only
        if ($LASTEXITCODE -ne 0) { Write-Warning "crab: bad-ass-mcp pull failed, running what is checked out" }
        if ($env:CRAB_INSTALL -eq "1") {
            # A uv tool install is already editable on the checkout; reinstalling
            # picks up dependency changes. Otherwise pip, into CRAB_PYTHON.
            $tools = & uv tool list 2>$null
            if ($tools -match '^bad-ass-mcp\b') {
                & uv tool install --editable "$repo[windows]" --force -q
                if ($LASTEXITCODE -ne 0) { Write-Warning "crab: uv tool install failed" }
            } else {
                $shown = & $py -m pip show bad-ass-mcp 2>$null
                if (-not ($shown -match '^Editable project location')) {
                    & $py -m pip install -q -e $repo
                    if ($LASTEXITCODE -ne 0) { Write-Warning "crab: editable install failed" }
                }
            }
        }
    }
    $extra = Join-Path $dir "update.ps1"
    if (Test-Path $extra) { & $extra }
}

$env:CRAB_NAME = $Name
$env:CRAB_HOME = $dir
while ($true) {
    Remove-Item $flag -ErrorAction SilentlyContinue
    Update-Crab
    if ($fresh) {
        & $claude --dangerously-skip-permissions --session-id $session @ClaudeArgs
        $fresh = $false
    } else {
        & $claude --dangerously-skip-permissions --resume $session @ClaudeArgs
    }
    if (-not (Test-Path $flag)) { break }
    Write-Host "crab: $Name restarting"
}
