# Imports the exported KL task definitions on the NEW machine - all DISABLED,
# so nothing can trade until Phase 5 cutover deliberately enables them.
# Run ELEVATED. See MIGRATION_RUNBOOK.md Phase 4.
#   .\migrate_import_tasks.ps1 -BundleDir C:\path\to\migration_export
param([Parameter(Mandatory = $true)][string]$BundleDir)

$taskDir = Join-Path $BundleDir "tasks"
if (-not (Test-Path $taskDir)) { throw "no tasks\ folder in $BundleDir" }

$imported = 0; $failed = @()
foreach ($f in Get-ChildItem $taskDir -Filter *.xml) {
    $name = $f.BaseName -replace '_', ' '   # best-effort; XML carries the real URI
    $xml = Get-Content $f.FullName -Raw
    # task name from the export is authoritative when present in the URI
    if ($xml -match '<URI>\\?([^<]+)</URI>') { $name = $Matches[1].TrimStart('\') }
    try {
        Register-ScheduledTask -TaskName $name -Xml $xml -Force -ErrorAction Stop | Out-Null
        Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
        $imported++
    } catch {
        $failed += "$name : $($_.Exception.Message)"
    }
}
""
"imported + disabled: $imported"
if ($failed) { "FAILED:"; $failed | ForEach-Object { "  $_" } }
""
Get-ScheduledTask -TaskName "KL *" -ErrorAction SilentlyContinue |
    Sort-Object TaskName | Format-Table TaskName, State -AutoSize
"All tasks must show Disabled. Enabling happens in Phase 5 step 4 - not before."
