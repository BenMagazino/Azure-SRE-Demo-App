<#
.SYNOPSIS
  NSG-lane connectivity fault, shipped FROM IaC.
#>
param(
  [string]$ResourceGroup = "rg-zava-learning-demo"
)
. "$PSScriptRoot\_common.ps1"

Write-Host "[break-nsg] Shipping the nsg-lane legacy DENY rule from IaC..." -ForegroundColor Yellow

Write-Host "  1/2 Committing bad release (injectLegacyDenyNsgLane=true) to GitHub..." -ForegroundColor Gray
$changed = Set-ParamLine -Pattern '"injectLegacyDenyNsgLane"\s*:\s*\{\s*"value"\s*:\s*false\s*\}' `
                         -Replacement '"injectLegacyDenyNsgLane": { "value": true }'
if ($changed) { Invoke-GitPush -Message "Add legacy cross-subnet segmentation rule to nsg lane" }
else { Write-Host "  (injectLegacyDenyNsgLane already true in source)" -ForegroundColor DarkGray }

Write-Host "  2/2 Adding legacy-cross-subnet-deny to the live nsg-lane NSG..." -ForegroundColor Gray
$token = Get-ResourceToken -ResourceGroup $ResourceGroup
$nsgName = "nsg-nsglane-$token"
az network nsg rule create `
  --resource-group $ResourceGroup `
  --nsg-name $nsgName `
  --name "legacy-cross-subnet-deny" `
  --priority 100 `
  --direction Inbound `
  --access Deny `
  --protocol "*" `
  --source-address-prefixes "10.20.1.0/24" `
  --source-port-ranges "*" `
  --destination-address-prefixes "*" `
  --destination-port-ranges "*" -o none
if ($LASTEXITCODE -ne 0) {
  throw "[break-nsg] Azure rejected the NSG rule; fault was not injected."
}
$rule = az network nsg rule show -g $ResourceGroup --nsg-name $nsgName `
  -n "legacy-cross-subnet-deny" --query "{access:access,priority:priority}" -o json |
  ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $rule.access -ne "Deny" -or $rule.priority -ne 100) {
  throw "[break-nsg] The live deny rule could not be verified; fault was not injected."
}

Write-Host "[break-nsg] Fault live + in source. The nsg lane can no longer launch quizzes." -ForegroundColor Red
New-PagerDutyIncident -Title "Zava quiz launches failing — students cannot start quizzes" `
  -Details "Students using the quiz lane on port 8081 cannot start quizzes. Demo monitoring observed the student-facing failure." | Out-Null
