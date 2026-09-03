<#
.SYNOPSIS
  Restores the secret-lane DB password from the real admin password secret.
#>
param(
  [string]$ResourceGroup = "rg-zava-learning-demo",
  [string]$AppName = "quiz-secret"
)
. "$PSScriptRoot\_common.ps1"

Write-Host "[fix-secret] Restoring the secret-lane DB password from the baseline secret..." -ForegroundColor Yellow
Copy-KvSecret -ResourceGroup $ResourceGroup -SourceName "db-password" -DestinationName "db-password-secretlane"

Write-Host "  Restoring and verifying the isolated secret-lane reference..." -ForegroundColor Gray
Ensure-SecretLaneReference -ResourceGroup $ResourceGroup -AppName $AppName

$previousRevision = az containerapp show -g $ResourceGroup -n $AppName `
  --query "properties.latestRevisionName" -o tsv --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $previousRevision) {
  throw "[fix-secret] Could not read the current revision before recovery."
}

Write-Host "  Forcing $AppName to re-read Key Vault secret state..." -ForegroundColor Gray
$stamp = Get-Date -Format "yyyyMMddHHmmssfff"
az containerapp update --resource-group $ResourceGroup --name $AppName --set-env-vars FORCE_ROTATE=$stamp -o none
if ($LASTEXITCODE -ne 0) {
  throw "[fix-secret] The Container App did not accept the secret refresh."
}

Wait-SecretLaneRecovery -ResourceGroup $ResourceGroup -AppName $AppName `
  -PreviousRevision $previousRevision

Write-Host "[fix-secret] Dedicated secret and reference restored; port 8087 returned HTTP 200." -ForegroundColor Green
