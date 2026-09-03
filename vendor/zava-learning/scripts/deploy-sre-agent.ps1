<#
.SYNOPSIS
  Provisions the Zava Learning SRE Agent against an existing lab resource group.
  Run this AFTER the main infra deployment. Discovers the App Insights / Log
  Analytics / managed-identity resources in the RG and deploys infra/modules/sre-agent.bicep.

.NOTES
  The lab infra is provisioned without the SRE Agent so the operator can provision
  it themselves (different access / cost considerations). This wrapper makes that a
  one-liner.
#>
param(
  [string]$ResourceGroup,
  [string]$Location,
  [string]$AgentName,
  [string]$EnvironmentName,
  [ValidateSet('PagerDuty','AzMonitor')]
  [string]$IncidentPlatform = "AzMonitor",
  [ValidateSet('Anthropic','MicrosoftFoundry')]
  [string]$ModelProvider = "Anthropic",
  [string]$ModelName = "Automatic"
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-AzTsv {
  param(
    [Parameter(Mandatory)][string[]]$Arguments,
    [Parameter(Mandatory)][string]$Description,
    [switch]$AllowEmpty
  )
  for ($attempt = 1; $attempt -le 4; $attempt++) {
    $result = & az @Arguments 2>$null
    $code = $LASTEXITCODE
    $value = ($result | Out-String).Trim()
    if ($code -eq 0 -and ($AllowEmpty -or $value)) {
      return $value
    }
    if ($attempt -lt 4) {
      Write-Host "Retrying $Description ($attempt/4)..." -ForegroundColor DarkYellow
      Start-Sleep -Seconds (10 * $attempt)
    }
  }
  throw "Azure CLI could not resolve $Description."
}

# Prompt for the core values (rg / model / region) if not supplied.
if (-not $ResourceGroup) { $ResourceGroup = Read-Host "Resource group (e.g. rg-zava-learning-demo)" }
if (-not $ModelProvider) {
  $mp = Read-Host "Model provider [Anthropic / MicrosoftFoundry] (default Anthropic)"
  $ModelProvider = if ($mp) { $mp } else { "Anthropic" }
}

if (-not $AgentName) {
  $envName = ($ResourceGroup -replace '^rg-zava-learning-', '')
  $AgentName = "sre-zava-learning-$envName"
}
if (-not $EnvironmentName) {
  $EnvironmentName = ($ResourceGroup -replace '^rg-zava-learning-', '')
}

Write-Host "Discovering lab resources in $ResourceGroup..." -ForegroundColor Cyan
$identityId = Invoke-AzTsv -Description "the dedicated SRE Agent identity" -Arguments @(
  "identity", "list", "-g", $ResourceGroup,
  "--query", "[?starts_with(name,'id-zava-agent-')].id | [0]", "-o", "tsv"
)
$aiName = Invoke-AzTsv -Description "Application Insights" -Arguments @(
  "resource", "list", "-g", $ResourceGroup,
  "--resource-type", "Microsoft.Insights/components",
  "--query", "[0].name", "-o", "tsv"
)
$aiId = Invoke-AzTsv -Description "the Application Insights resource ID" -Arguments @(
  "resource", "show", "-g", $ResourceGroup, "-n", $aiName,
  "--resource-type", "Microsoft.Insights/components",
  "--query", "id", "-o", "tsv"
)
$aiAppId = Invoke-AzTsv -Description "the Application Insights App ID" -Arguments @(
  "resource", "show", "-g", $ResourceGroup, "-n", $aiName,
  "--resource-type", "Microsoft.Insights/components",
  "--query", "properties.AppId", "-o", "tsv"
)
$aiConn = Invoke-AzTsv -Description "the Application Insights connection string" -Arguments @(
  "resource", "show", "-g", $ResourceGroup, "-n", $aiName,
  "--resource-type", "Microsoft.Insights/components",
  "--query", "properties.ConnectionString", "-o", "tsv"
)
$postgresId = Invoke-AzTsv -Description "PostgreSQL" -Arguments @(
  "postgres", "flexible-server", "list", "-g", $ResourceGroup,
  "--query", "[0].id", "-o", "tsv"
)
$rgId = Invoke-AzTsv -Description "the resource group ID" -Arguments @(
  "group", "show", "-n", $ResourceGroup, "--query", "id", "-o", "tsv"
)
if (-not $Location) {
  $Location = Invoke-AzTsv -Description "the resource group location" -Arguments @(
    "group", "show", "-n", $ResourceGroup, "--query", "location", "-o", "tsv"
  )
}
$identityPrincipalId = Invoke-AzTsv -Description "the SRE Agent identity principal" -Arguments @(
  "identity", "show", "--ids", $identityId, "--query", "principalId", "-o", "tsv"
)
$subscriptionId = Invoke-AzTsv -Description "the Azure subscription" -Arguments @(
  "account", "show", "--query", "id", "-o", "tsv"
)
$postgresStartRoleDefinitionId = "/subscriptions/$subscriptionId/providers/Microsoft.Authorization/roleDefinitions/f8717311-09b5-4153-8abe-edb3c595c35f"
$existingPostgresStartAssignment = Invoke-AzTsv `
  -Description "existing PostgreSQL start-role assignments" `
  -AllowEmpty `
  -Arguments @(
    "role", "assignment", "list",
    "--assignee-object-id", $identityPrincipalId,
    "--scope", $postgresId,
    "--fill-principal-name", "false",
    "--query", "[?roleDefinitionId=='$postgresStartRoleDefinitionId'].id | [0]",
    "-o", "tsv"
  )
$createPostgresStartAssignment = if ($existingPostgresStartAssignment) { "false" } else { "true" }
if ($existingPostgresStartAssignment) {
  Write-Host "Reusing the existing PostgreSQL start-role assignment." -ForegroundColor DarkGray
}

Write-Host "Deploying SRE Agent '$AgentName' (platform: $IncidentPlatform)..." -ForegroundColor Cyan
$deploymentOutputs = az deployment group create `
  --resource-group $ResourceGroup `
  --name "sre-agent-$(Get-Date -Format 'yyyyMMddHHmmss')" `
  --template-file (Join-Path $repoRoot "infra/modules/sre-agent.bicep") `
  --parameters `
      location=$Location `
      agentName=$AgentName `
      environmentName=$EnvironmentName `
      identityId=$identityId `
      appInsightsAppId=$aiAppId `
      appInsightsConnectionString=$aiConn `
      appInsightsId=$aiId `
      managedResourceGroupId=$rgId `
      postgresServerId=$postgresId `
      createPostgresStartAssignment=$createPostgresStartAssignment `
      incidentPlatform=$IncidentPlatform `
      modelProvider=$ModelProvider `
      modelName=$ModelName `
  --query "properties.outputs" -o json
if ($LASTEXITCODE -ne 0) {
  throw "SRE Agent deployment failed. Review the Azure deployment error above."
}
$deploymentOutputs

Write-Host "SRE Agent provisioned (deployment only)." -ForegroundColor Green
Write-Host "Next: apply agent CONFIG (connectors/skills/incident filter/KB/tools) via Azure MCP -" -ForegroundColor Yellow
Write-Host "      see sre-config/agent-config/README.md" -ForegroundColor Yellow
