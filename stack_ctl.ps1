<#
ESG stack control -- standby / start / status

Why this exists:
  The watchdog task (ESG-Tunnel-Watchdog) runs every minute and will call
  "docker start cloudflared-esg" whenever the tunnel container is stopped but
  Docker is alive. So stopping the tunnel by hand does NOT keep it down.
  Standby mode therefore disables the watchdog FIRST, then stops containers.

Usage:
  powershell -ExecutionPolicy Bypass -NoProfile -File stack_ctl.ps1 -Action Status
  powershell -ExecutionPolicy Bypass -NoProfile -File stack_ctl.ps1 -Action Standby
  powershell -ExecutionPolicy Bypass -NoProfile -File stack_ctl.ps1 -Action Start

Pure ASCII on purpose: Windows PowerShell 5.1 reads .ps1 as GBK by default,
non-ASCII text becomes mojibake and turns into syntax errors.
#>
param(
    [ValidateSet('Status', 'Standby', 'Start', 'StartQuick')]
    [string]$Action = 'Status'
)

$ErrorActionPreference = 'Continue'
# Keep Invoke-WebRequest's progress stream out of stdout: when this script is run
# with output redirected to a file, the progress records otherwise bury the result.
$ProgressPreference = 'SilentlyContinue'

$ProjectDir = '[PROJECT]'
$TunnelDir  = '[WORKSPACE]'
$TunnelFile = 'cloudflared-esg-tunnel.yml'
$TaskName   = 'ESG-Tunnel-Watchdog'
$Containers = @('esg-frontend', 'esg-extractor', 'esg-agent2', 'esg-one-api', 'cloudflared-esg')
$ReadyUrl   = 'http://127.0.0.1:20241/ready'
$PublicUrl  = 'https://esg.[DEPLOY_HOST]/'

# --- 2026-09-14 added: temporary quick tunnel (Wednesday test), see cloudflared-esg-quick.yml ---
# Why: esg.[DEPLOY_HOST] is under ICP review; a domain must NOT resolve before the
#      first filing is approved. The quick tunnel uses a random *.trycloudflare.com
#      hostname, so it never touches our own domain.
# Additive only: Start / Standby / Status keep their original behaviour. Standby just
#      stops one extra container (the quick tunnel) so standby never leaves an open
#      tunnel behind.
$QuickTunnelFile = 'cloudflared-esg-quick.yml'
$NamedContainer  = 'cloudflared-esg'
$QuickContainer  = 'cloudflared-esg-quick'
$QuickReadyUrl   = 'http://127.0.0.1:20242/ready'
$StopContainers  = @('esg-frontend', 'esg-extractor', 'esg-agent2', 'esg-one-api', 'cloudflared-esg', 'cloudflared-esg-quick')

function Test-Docker {
    try {
        $null = & docker info --format '{{.ServerVersion}}' 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-OneLine($value) {
    return ($value | Out-String).Trim()
}

# Pull the current trycloudflare URL out of the quick tunnel container logs.
# NOTE: do NOT build the string with Out-String here. Out-String hard-wraps long
#       lines at the console width (~80 chars), which splits the URL mid-token and
#       makes the regex fail silently. Joining per-item ToString() keeps lines whole.
function Get-QuickUrl {
    try {
        $raw = & docker logs $QuickContainer --tail 200 2>&1
    } catch {
        return ''
    }
    $joined = (@($raw) | ForEach-Object { $_.ToString() }) -join "`n"
    $m = [regex]::Match($joined, 'https://[a-z0-9-]+\.trycloudflare\.com')
    if ($m.Success) { return $m.Value }
    return ''
}

function Show-Status {
    Write-Host '=== Docker ==='
    if (Test-Docker) {
        Write-Host ('  OK  server=' + (Get-OneLine (& docker info --format '{{.ServerVersion}}' 2>&1)))
    } else {
        Write-Host '  NOT reachable (Docker Desktop is probably stopped)'
    }

    Write-Host ''
    Write-Host '=== Containers ==='
    if (Test-Docker) {
        foreach ($c in $Containers) {
            $s = Get-OneLine (& docker ps -a --filter "name=^/$c$" --format '{{.Status}}' 2>&1)
            if ([string]::IsNullOrWhiteSpace($s)) { $s = '(not created)' }
            Write-Host ('  {0,-18} {1}' -f $c, $s)
        }
    } else {
        Write-Host '  (cannot list - docker down)'
    }

    Write-Host ''
    Write-Host '=== Tunnel ready endpoint ==='
    try {
        $r = Invoke-WebRequest -Uri $ReadyUrl -UseBasicParsing -TimeoutSec 6
        Write-Host ('  HTTP ' + $r.StatusCode + '  ' + $r.Content)
    } catch {
        Write-Host ('  DOWN - ' + $_.Exception.Message)
    }

    Write-Host ''
    Write-Host '=== Watchdog task ==='
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        $i = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ('  State=' + $t.State + '  LastRun=' + $i.LastRunTime + '  NextRun=' + $i.NextRunTime)
        if ($t.State -ne 'Ready') {
            Write-Host '  NOTE: task is not Ready -- it may be Disabled (this is normal in standby).'
        }
    } else {
        Write-Host '  (task not registered)'
    }

    Write-Host ''
    Write-Host '=== Public URL ==='
    try {
        $p = Invoke-WebRequest -Uri $PublicUrl -UseBasicParsing -TimeoutSec 20
        Write-Host ('  HTTP ' + $p.StatusCode)
    } catch {
        Write-Host ('  UNREACHABLE - ' + $_.Exception.Message)
    }

    # 2026-09-14: during ICP review the DNS record for this hostname is deleted on
    # purpose, so UNREACHABLE here is the expected (compliant) state.
    Write-Host ''
    Write-Host '=== Quick tunnel (Wednesday test entry) ==='
    if (Test-Docker) {
        $qs = Get-OneLine (& docker ps -a --filter "name=^/$QuickContainer$" --format '{{.Status}}' 2>&1)
        if ([string]::IsNullOrWhiteSpace($qs)) { $qs = '(not created)' }
        Write-Host ('  ' + $QuickContainer + '  ' + $qs)
        $qu = Get-QuickUrl
        if ($qu) {
            Write-Host ('  URL  ' + $qu)
        } else {
            Write-Host '  URL  (none in logs - start it with -Action StartQuick)'
        }
    } else {
        Write-Host '  (cannot list - docker down)'
    }
}

function Enter-Standby {
    Write-Host '[1/2] Disabling watchdog task (must be first, see header comment) ...'
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host '      disabled'
    } else {
        Write-Host '      (not registered - nothing to do)'
    }

    Write-Host '[2/2] Stopping containers ...'
    if (-not (Test-Docker)) {
        Write-Host '      docker not reachable - nothing to stop'
    } else {
        foreach ($c in $StopContainers) {
            $exists = Get-OneLine (& docker ps -a --filter "name=^/$c$" --format '{{.Names}}' 2>&1)
            if ([string]::IsNullOrWhiteSpace($exists)) {
                Write-Host ('      {0,-18} not created' -f $c)
                continue
            }
            $running = Get-OneLine (& docker ps --filter "name=^/$c$" --format '{{.Names}}' 2>&1)
            if ([string]::IsNullOrWhiteSpace($running)) {
                Write-Host ('      {0,-18} already stopped' -f $c)
                continue
            }
            $null = & docker stop $c 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host ('      {0,-18} stopped' -f $c)
            } else {
                Write-Host ('      {0,-18} STOP FAILED' -f $c)
            }
        }
    }

    Write-Host ''
    Write-Host 'Standby complete. Run with -Action Start when you need it again.'
}

function Start-Stack {
    Write-Host '[1/4] Checking Docker ...'
    if (-not (Test-Docker)) {
        Write-Host '      Docker is not running. Start Docker Desktop, then re-run -Action Start.'
        exit 1
    }
    Write-Host '      OK'

    Write-Host '[2/4] Starting application containers ...'
    Push-Location $ProjectDir
    $null = & docker-compose up -d 2>&1
    Write-Host ('      docker-compose up -d  exit=' + $LASTEXITCODE)
    Pop-Location

    Write-Host '[3/4] Starting tunnel container ...'
    $tunnelPath = Join-Path $TunnelDir $TunnelFile
    $null = & docker-compose -f $tunnelPath up -d 2>&1
    Write-Host ('      docker-compose -f tunnel up -d  exit=' + $LASTEXITCODE)

    Write-Host '[4/4] Enabling watchdog, waiting for the tunnel to register ...'
    Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    $ok = $false
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 3
        try {
            $r = Invoke-WebRequest -Uri $ReadyUrl -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) {
                Write-Host ('      ready after ' + ($i * 3) + 's -> ' + $r.Content)
                $ok = $true
                break
            }
        } catch { }
        Write-Host ('      waiting ... ' + $i + '/20')
    }
    if (-not $ok) {
        Write-Host '      tunnel did NOT report ready in 60s - check: docker logs cloudflared-esg --tail 40'
    }

    Write-Host ''
    Write-Host '=== Public check ==='
    try {
        $p = Invoke-WebRequest -Uri $PublicUrl -UseBasicParsing -TimeoutSec 25
        Write-Host ('  ' + $PublicUrl + ' -> HTTP ' + $p.StatusCode)
    } catch {
        Write-Host ('  ' + $PublicUrl + ' -> UNREACHABLE - ' + $_.Exception.Message)
    }
    try {
        $h = Invoke-WebRequest -Uri ($PublicUrl.TrimEnd('/') + '/api/health') -UseBasicParsing -TimeoutSec 25
        Write-Host ('  /api/health -> HTTP ' + $h.StatusCode)
    } catch {
        Write-Host ('  /api/health -> ERR - ' + $_.Exception.Message)
    }
    Write-Host ''
    Write-Host 'Start complete.'
}

# ---------------------------------------------------------------------------
# StartQuick (added 2026-09-14) -- Wednesday-test entry
# App containers + TEMPORARY quick tunnel (*.trycloudflare.com).
# Deliberately does NOT start the named tunnel, and disables the watchdog first:
# the named tunnel publishes esg.[DEPLOY_HOST], a domain currently under ICP
# review that must not resolve until the review passes.
# ---------------------------------------------------------------------------
function Start-QuickStack {
    Write-Host '[1/6] Checking Docker ...'
    if (-not (Test-Docker)) {
        Write-Host '      Docker is not running. Start Docker Desktop, then re-run -Action StartQuick.'
        exit 1
    }
    Write-Host '      OK'

    Write-Host '[2/6] Disabling watchdog task (it would auto-start the NAMED tunnel) ...'
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host '      disabled'
    } else {
        Write-Host '      (not registered - nothing to do)'
    }

    Write-Host '[3/6] Ensuring the NAMED tunnel is stopped ...'
    $namedUp = Get-OneLine (& docker ps --filter "name=^/$NamedContainer$" --format '{{.Names}}' 2>&1)
    if ([string]::IsNullOrWhiteSpace($namedUp)) {
        Write-Host ('      ' + $NamedContainer + ' already stopped')
    } else {
        $null = & docker stop $NamedContainer 2>&1
        Write-Host ('      ' + $NamedContainer + ' stopped')
    }

    Write-Host '[4/6] Starting application containers ...'
    Push-Location $ProjectDir
    $null = & docker-compose up -d 2>&1
    Write-Host ('      docker-compose up -d  exit=' + $LASTEXITCODE)
    Pop-Location

    Write-Host '[5/6] Recreating quick tunnel (fresh URL every run) ...'
    $null = & docker rm -f $QuickContainer 2>&1
    $quickPath = Join-Path $TunnelDir $QuickTunnelFile
    $null = & docker-compose -f $quickPath up -d 2>&1
    Write-Host ('      docker-compose -f quick up -d  exit=' + $LASTEXITCODE)

    Write-Host '[6/6] Waiting for the trycloudflare URL ...'
    $url = ''
    for ($i = 1; $i -le 30; $i++) {
        Start-Sleep -Seconds 3
        $url = Get-QuickUrl
        if ($url) {
            Write-Host ('      got it after ' + ($i * 3) + 's')
            break
        }
        Write-Host ('      waiting ... ' + $i + '/30')
    }
    if (-not $url) {
        Write-Host '      NO URL after 90s. Inspect: docker logs cloudflared-esg-quick --tail 60'
        Write-Host ''
        return
    }

    Write-Host ''
    Write-Host '=============================================='
    Write-Host ('  PUBLIC URL:  ' + $url)
    Write-Host '=============================================='
    Write-Host ''
    Write-Host '=== Public check (a fresh quick hostname needs ~40s to propagate) ==='
    $ok = $false
    for ($j = 1; $j -le 8; $j++) {
        try {
            $p = Invoke-WebRequest -Uri ($url + '/') -UseBasicParsing -TimeoutSec 25
            Write-Host ('  /            -> HTTP ' + $p.StatusCode + '  (' + $p.RawContentLength + ' bytes)')
            $ok = $true
            break
        } catch {
            Write-Host ('  attempt ' + $j + '/8 -> ' + $_.Exception.Message)
            Start-Sleep -Seconds 10
        }
    }
    if ($ok) {
        try {
            $h = Invoke-WebRequest -Uri ($url + '/api/health') -UseBasicParsing -TimeoutSec 25
            Write-Host ('  /api/health  -> HTTP ' + $h.StatusCode)
        } catch {
            Write-Host ('  /api/health  -> ERR - ' + $_.Exception.Message)
        }
    } else {
        Write-Host '  Still unreachable after ~80s. Inspect: docker logs cloudflared-esg-quick --tail 30'
    }
    Write-Host ''
    Write-Host ('Send this link to teammates:  ' + $url)
    Write-Host 'StartQuick complete.'
}

switch ($Action) {
    'Status'     { Show-Status }
    'Standby'    { Enter-Standby }
    'Start'      { Start-Stack }
    'StartQuick' { Start-QuickStack }
}
