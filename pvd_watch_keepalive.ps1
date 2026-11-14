# Keepalive for the wave dashboards (imm_pvd_watch.py), run first by the
# "KL pvd-scan waves" task every 15 min. Jack 2026-11-07: "Yes, add it" -- the
# dashboards were started by hand and die with a reboot; bank-early sizing
# (pvd_wave_tune.py) reads the 8773 dashboard's history.json, so a dead or hung
# 8773 would leave every 2028 market at 200 a side.
# 2026-11-14 (Jack: "Yes, do 1-5"): the 8773 (2028 since 11/06) and 8771 (political
# 11/06) dashboards are RETIRED -- their markets are registry waves with their own
# dashboards (w1111-elec2028 / w1111-w28 / w1111-pol); history stays on disk.
#
# A dashboard is (re)started when its port isn't listening, or when it listens
# but its history.json hasn't been written for $StaleMin minutes (a hung poller).
# Add a wave: one more entry in $Dash (port, history dir, go-live, exact series).
$ErrorActionPreference = 'Continue'
$KL = 'C:\Users\jackd\Documents\KL'
$Py = 'C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe'
$StaleMin = 40
$Dash = @(
    # the 11/04 election wave = the list less the 11/06 waves (Exclude); fresh history from 02:26:40Z
    # (the old pvd-watch history was days stale: its first poll would have credited days at today's rate)
    @{ Port = 8774; Dir = 'pvd-watch-1104b'; GoLive = '2026-11-07T02:26:40Z';
       Exclude = 'KXHOUSERACE,KXDPRESPRIMARY,KXRPRESPRIMARY,KXPRESSTATE,KXPRESDISTRICT,KXPOPVOTE,KXGOVRACE,KXCHINA1,KXDPRESPRIMWIN,KXGOPDELMAJ,KXRPRESPRIMWIN,KXDEMDELMAJ,KXRPRESWINCOUNT,KXPAMINWAGE,KXDPRESWINCOUNT,KXRPRESSWEEP,KXTRUMPFIRSTENDORSE,KXGOPFIRSTWINNOM,KXJEFFRIESFIRST,KXDEMFIRSTWINNOM,KXWIMEDICAIDEXP,KXDPRESSWEEP,KXTHIRDPARTYSTATEWIN,KXRPRESFRACTURED,KXDPRESFRACTURED,KXDCSTATEHOODHOUSE,KXGOPIMPEACHVOTES,KXGOVTSHUTLENGTH,KXHAGCHAIR,KXHEDUCATIONCHAIR,KXHJUDICIARYRM,KXHOUSEEXPANDLAW,KXITALYEXITTAX,KXJUDGECOUNTYEAR,KXNPVICSTATES,KXNRCCCHAIR,KXOVERSIGHTRM,KXPRSTATEHOUSE,KXROEHOUSE,KXSBANKINGCHAIR,KXSCOTUSEXPANDHOUSE,KXSCOTUSEXPANDLAW,KXSCOTUSTERMHOUSE,KXSHELPRM,KXSJUDICIARYRM,KXTARIFFREPEAL,KXTRUMPGOPCONVICT,KXTPPRESPCT,KXTRUMPNOMNUM,KXWIRTWREPEAL,KXMINWAGEHOUSE,KXGAMEDICAIDEXP,KXKSMEDICAIDEXP,KXACASUBSIDYHOUSE,KXMEDICAIDWORKHOUSE,KXWIMARIJUANA,KXPUBLICOPTIONHOUSE,KXPAMARIJUANA' }
)
# the crypto after-events page (Jack 2026-11-14: "Yes, keep it alive"): imm_pvd_watch's page run by
# its own wrapper, which picks its markets from the programs feed (Script = the file to start).
# Ahead of the registry waves, so registry wave w1114-cryptoev (port 8783) keeps this page.
$Dash += @{ Port = 8783; Dir = 'crypto-event-watch'; GoLive = '2026-11-14T01:09:35Z'; Script = 'imm_crypto_event_watch.py' }
# Registry waves (pvd_waves.py add, the market-wave playbook): one dashboard each, and their
# series kept off the catch-all (Exclude) dashboards.
$RegFile = Join-Path $KL 'run-logs\pvd-scan\waves.json'
$RegSeries = @()
if (Test-Path $RegFile) {
    try {
        $reg = Get-Content $RegFile -Raw | ConvertFrom-Json
        foreach ($p in $reg.waves.PSObject.Properties) {
            $w = $p.Value
            if ($w.enabled -eq $false) { continue }
            $all = @($w.series) + @($w.near) | Where-Object { $_ }
            $RegSeries += $all
            # a port a fixed entry above already serves keeps that entry's page (w1114-cryptoev
            # banks off the crypto page on 8783)
            if (@($Dash | Where-Object { $_.Port -eq [int]$w.port }).Count) { continue }
            $Dash += @{ Port = [int]$w.port; Dir = [string]$w.dir; GoLive = [string]$w.go_live; Series = ($all -join ',') }
        }
    } catch { }
}
# phone access on the home network (imm_pvd_watch PHONE ACCESS): key in run-logs\pvd-scan\dash_token.txt
$TokFile = Join-Path $KL 'run-logs\pvd-scan\dash_token.txt'
if (Test-Path $TokFile) {
    $env:PVD_WATCH_TOKEN = (Get-Content $TokFile -Raw).Trim()
    $env:PVD_WATCH_HOST = '0.0.0.0'
}
$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
# The task calls this with output to nul and it logs here itself: a dashboard
# started from a shell whose stdout is redirected to the task log inherits that
# handle and holds the log open, and every later ">> log" pass then fails.
$LogFile = Join-Path $KL 'run-logs\pvd-scan\waves-task.log'
function Say($m) { try { Add-Content -Path $LogFile -Value $m -ErrorAction Stop } catch {} ; $m }
foreach ($d in $Dash) {
    $dir = Join-Path $KL ('run-logs\' + $d.Dir)
    $hist = Join-Path $dir 'history.json'
    $listening = [bool](Get-NetTCPConnection -LocalPort $d.Port -State Listen -ErrorAction SilentlyContinue)
    $ageMin = if (Test-Path $hist) { ((Get-Date) - (Get-Item $hist).LastWriteTime).TotalMinutes } else { 1e9 }
    if ($listening -and $ageMin -le $StaleMin) {
        Say "$stamp keepalive :$($d.Port) ok (history $([math]::Round($ageMin)) min old)"
        continue
    }
    $why = if (-not $listening) { 'not listening' } else { "history $([math]::Round($ageMin)) min old (hung)" }
    $script = if ($d.Script) { $d.Script } else { 'imm_pvd_watch.py' }
    # a hung one still holds its port: stop that exact dashboard first
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*$([IO.Path]::GetFileNameWithoutExtension($script))*" -and $_.CommandLine -like "*--port*$($d.Port)*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $env:PVD_WATCH_GO_LIVE = $d.GoLive
    $env:PVD_WATCH_DIR = $dir
    if ($d.Series) { $env:PVD_WATCH_SERIES = $d.Series } else { Remove-Item Env:PVD_WATCH_SERIES -ErrorAction SilentlyContinue }
    if ($d.Exclude) {
        $ex = $d.Exclude
        if ($RegSeries.Count) { $ex = $ex + ',' + ($RegSeries -join ',') }
        $env:PVD_WATCH_EXCLUDE = $ex
    } else { Remove-Item Env:PVD_WATCH_EXCLUDE -ErrorAction SilentlyContinue }
    $p = Start-Process -FilePath $Py -ArgumentList (Join-Path $KL $script), '--port', "$($d.Port)" `
        -WorkingDirectory $KL -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dir 'watch.out.log') -RedirectStandardError (Join-Path $dir 'watch.err.log')
    Say "$stamp keepalive :$($d.Port) $why -> started pid $($p.Id)"
}
