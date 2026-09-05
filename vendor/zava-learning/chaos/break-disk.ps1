<#
.SYNOPSIS
  Disk scenario: fills the reporting-worker VM's data disk (/data) so the nightly
  grade-export job can no longer write its export files and fails with "No space left
  on device". This is live operational drift on the VM (no IaC change): the export
  worker keeps running on its timer but every cycle now fails, which the symptom-only
  Azure Monitor grade-exports-failing alert pages on. The SRE Agent must diagnose
  the disk-pressure root cause from Syslog / disk telemetry and free space (fix-disk.ps1).
#>
param(
  [string]$ResourceGroup = "rg-zava-learning-demo"
)
. "$PSScriptRoot\_common.ps1"

$vm = Get-ReportingVmName -ResourceGroup $ResourceGroup
Write-Host "[break-disk] Filling /data on $vm so grade exports start failing..." -ForegroundColor Yellow

# The export worker only runs when the VM is up; ensure it is running (and not deallocated)
# before we fill the disk, otherwise the fault would silently no-op.
$power = az vm show -d -g $ResourceGroup -n $vm --query powerState -o tsv 2>$null
if ($power -ne "VM running") {
  Write-Host "  VM power state is '$power' — starting it before injecting the fault..." -ForegroundColor Gray
  az vm start -g $ResourceGroup -n $vm -o none
  if ($LASTEXITCODE -ne 0) {
    throw "[break-disk] Could not start $vm (power state was '$power'). The grade-export worker cannot run, so the disk-pressure fault was NOT injected. Resolve the VM start failure (e.g. SKU/capacity) and re-run."
  }
  Start-Sleep -Seconds 60
}

$script = "sudo bash -c 'dd if=/dev/zero of=/data/exports/backlog.bin bs=1M 2>&1; df -h /data; systemctl start zava-export.service || true; sleep 3; journalctl -t zava-export --since=-2min --no-pager -n 20'"
$out = az vm run-command invoke -g $ResourceGroup -n $vm --command-id RunShellScript --scripts "$script" `
  --query "value[0].message" -o tsv
# az returns the multi-line message as a string[] in PowerShell; collapse to one string so the
# checks below are boolean tests, not array filters.
$out = ($out | Out-String)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($out)) {
  throw "[break-disk] The disk-fill command failed on $vm (no output). Fault NOT injected — check the VM and /data mount."
}
Write-Host $out

# Confirm the fault actually took effect: the disk must be full (dd hit ENOSPC or df shows 100%).
if ($out -notmatch "No space left on device" -and $out -notmatch "100%") {
  throw "[break-disk] /data did NOT fill up on $vm (no ENOSPC / not 100% — see output above). The fault did not take effect; the grade exports will keep succeeding. Verify the dedicated 8 GB data disk is mounted at /data and re-run."
}

if ($out -notmatch "zava-export" -or $out -notmatch "FAILED") {
  throw "[break-disk] The disk filled, but an immediate failed zava-export telemetry event was not confirmed."
}

Write-Host "[break-disk] Data disk filled and failed export telemetry emitted." -ForegroundColor Red
