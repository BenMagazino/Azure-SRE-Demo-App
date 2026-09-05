<#
.SYNOPSIS
  Query lane fix: rebuilds the question_bank index on zava_query (the equivalent of a REINDEX
  after index corruption) so quiz lookups use the index again instead of a full table scan.
#>
param(
  [string]$ResourceGroup = "rg-zava-learning-demo"
)
. "$PSScriptRoot\_common.ps1"

Write-Host "[fix-query] Rebuilding the question_bank index on zava_query (REINDEX/rebuild)..." -ForegroundColor Yellow
Invoke-PgSql -ResourceGroup $ResourceGroup -Database "zava_query" `
  -FilePath "src\db\restore-question-bank.sql"

Write-Host "[fix-query] Index rebuilt. Port 8085 quiz loading should recover." -ForegroundColor Green
