"""restart_imm.ps1 -Task never kills a bot before asking it to hand over.

2026-11-07 01:53Z: a code-change exit had just written restart_handoff.json
and its launcher was in the 30s relaunch sleep when -Task ran. With no bot
python up the handover step was skipped; the relaunched python took the
handoff file and the sweep after Stop-ScheduledTask killed it mid-adoption,
so the fresh task's bot cancelled the whole book (5,449 orders).

Each case dot-sources the script with -LibraryOnly (its main body never
runs) into a PowerShell harness that replaces every process, task and sleep
call with a simulation, then runs Invoke-TaskRestart and reports what it
did. Nothing here touches the live bot.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "restart_imm.ps1")

HARNESS = r'''
param([string]$Script, [string]$Dir, [string]$Scenario)
. $Script -LibraryOnly
$StatusDir = $Dir
$LogPath = Join-Path $Dir 'restart-imm.log'
$TaskName = 'KL test task (simulated)'
$HandoffWaitSecs = 600
$NoHandoff = $false
$global:Procs = New-Object System.Collections.ArrayList
$global:Events = New-Object System.Collections.ArrayList
$global:SpawnOnTick = $null     # a bot the launcher starts at the next sleep, if it is alive
$global:StopSpawns = $null      # a bot that appears when the task is stopped (the watchdog)
$global:HandsOver = $true       # a bot hands over when asked
function P($id, $name, $rank) { [pscustomobject]@{ ProcessId = $id; Name = $name; Rank = $rank } }
function Get-ImmChain { @($global:Procs | Sort-Object Rank) }
function Get-BotProcs { @($global:Procs | Where-Object { $_.Rank -eq 3 }) }
function Stop-Process {
    param([int]$Id, [switch]$Force)
    [void]$global:Events.Add("kill $Id")
    foreach ($x in @($global:Procs | Where-Object { $_.ProcessId -eq $Id })) { $global:Procs.Remove($x) }
}
function Get-ScheduledTask { param($TaskName) [pscustomobject]@{ TaskName = $TaskName } }
function Stop-ScheduledTask {
    param($TaskName)
    [void]$global:Events.Add("stop-task")
    if ($global:StopSpawns) {
        [void]$global:Procs.Add($global:StopSpawns)
        [void]$global:Events.Add("spawn $($global:StopSpawns.ProcessId)")
        $global:StopSpawns = $null
    }
}
function Start-ScheduledTask { param($TaskName) [void]$global:Events.Add("start-task") }
function Start-Sleep {
    param([int]$Seconds)
    if ($global:SpawnOnTick -and @($global:Procs | Where-Object { $_.Rank -eq 0 }).Count -gt 0) {
        [void]$global:Procs.Add($global:SpawnOnTick)
        [void]$global:Events.Add("spawn $($global:SpawnOnTick.ProcessId)")
        $global:SpawnOnTick = $null
    }
    $req = Join-Path $StatusDir 'restart_handoff_request.json'
    if ($global:HandsOver -and (Test-Path $req)) {
        foreach ($b in @($global:Procs | Where-Object { $_.Rank -eq 3 })) {
            $global:Procs.Remove($b)
            [void]$global:Events.Add("handover $($b.ProcessId)")
            Set-Content -Path (Join-Path $StatusDir 'restart_handoff.json') -Value '{}'
        }
    }
}
switch ($Scenario) {
    'gap_pending' {
        # the 11/07 case: the bot has exited with its book in the file, the
        # launcher sleeps and would start a bot at its next tick
        Set-Content -Path (Join-Path $Dir 'restart_handoff.json') -Value '{}'
        [void]$global:Procs.Add((P 100 'powershell.exe' 0))
        [void]$global:Procs.Add((P 300 'wscript.exe' 2))
        $global:SpawnOnTick = (P 200 'python.exe' 3)
    }
    'gap_spawned' {
        # the launcher has just started that bot, which is adopting the file
        [void]$global:Procs.Add((P 100 'powershell.exe' 0))
        [void]$global:Procs.Add((P 110 'cmd.exe' 1))
        [void]$global:Procs.Add((P 200 'python.exe' 3))
    }
    'watchdog_after_stop' {
        [void]$global:Procs.Add((P 100 'powershell.exe' 0))
        [void]$global:Procs.Add((P 200 'python.exe' 3))
        $global:StopSpawns = (P 400 'python.exe' 3)
    }
    'no_handoff' {
        $NoHandoff = $true
        [void]$global:Procs.Add((P 100 'powershell.exe' 0))
        [void]$global:Procs.Add((P 200 'python.exe' 3))
    }
    'stuck_bot' {
        $global:HandsOver = $false
        $HandoffWaitSecs = 1
        [void]$global:Procs.Add((P 100 'powershell.exe' 0))
        [void]$global:Procs.Add((P 200 'python.exe' 3))
    }
}
Invoke-TaskRestart
$log = ''
if (Test-Path $LogPath) { $log = [System.IO.File]::ReadAllText($LogPath) }
@{ events = @($global:Events); left = @($global:Procs | ForEach-Object { $_.ProcessId });
   handoff_file = (Test-Path (Join-Path $Dir 'restart_handoff.json')); log = $log } | ConvertTo-Json -Compress
'''


@unittest.skipUnless(shutil.which("powershell"), "needs Windows PowerShell")
class TestTaskRestartHandover(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="restart_imm_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.harness = os.path.join(self.dir, "harness.ps1")
        with open(self.harness, "w", encoding="utf-8") as f:
            f.write(HARNESS)

    def run_case(self, scenario):
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.harness,
             "-Script", SCRIPT, "-Dir", self.dir, "-Scenario", scenario],
            capture_output=True, text=True, timeout=120)
        lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, p.stdout + p.stderr)
        return json.loads(lines[-1])

    def test_a_handoff_waiting_in_the_relaunch_gap_is_kept(self):
        r = self.run_case("gap_pending")
        ev = r["events"]
        self.assertIn("kill 100", ev)
        self.assertIn("kill 300", ev)
        self.assertNotIn("spawn 200", ev)          # the launcher died before its relaunch
        self.assertNotIn("kill 200", ev)
        self.assertTrue(r["handoff_file"])         # the book waits for the fresh task's bot
        self.assertEqual(ev[-1], "start-task")
        self.assertIn("it stays for the fresh task's bot", r["log"])

    def test_a_bot_the_launcher_just_started_hands_over_first(self):
        r = self.run_case("gap_spawned")
        ev = r["events"]
        self.assertLess(ev.index("kill 100"), ev.index("handover 200"))
        self.assertLess(ev.index("handover 200"), ev.index("stop-task"))
        self.assertNotIn("kill 200", ev)
        self.assertTrue(r["handoff_file"])
        self.assertEqual(ev[-1], "start-task")

    def test_a_bot_found_by_the_sweep_is_asked_before_it_is_killed(self):
        r = self.run_case("watchdog_after_stop")
        ev = r["events"]
        self.assertIn("handover 200", ev)
        self.assertLess(ev.index("spawn 400"), ev.index("handover 400"))
        self.assertNotIn("kill 200", ev)
        self.assertNotIn("kill 400", ev)
        self.assertEqual(r["left"], [])
        self.assertEqual(ev[-1], "start-task")

    def test_no_handoff_still_hard_kills(self):
        r = self.run_case("no_handoff")
        ev = r["events"]
        self.assertIn("kill 200", ev)
        self.assertFalse(any(e.startswith("handover") for e in ev))
        self.assertEqual(ev[-1], "start-task")

    def test_a_bot_that_never_hands_over_is_killed_at_the_deadline(self):
        r = self.run_case("stuck_bot")
        ev = r["events"]
        self.assertIn("kill 200", ev)
        self.assertEqual(r["left"], [])
        self.assertEqual(ev[-1], "start-task")
        self.assertEqual(r["log"].count("bot still running after 1 s"), 1)   # asked once


if __name__ == "__main__":
    unittest.main()
