// Symptom-only alert rules + PagerDuty action group.
// IMPORTANT: alert names/descriptions describe the OBSERVED SYMPTOM only and must
// never reveal the root cause (NSG / LB / AppGW / app) — that is the SRE Agent's job.
@description('Azure region for the alert rules (must be a real region, not global).')
param location string
@description('Resource name suffix token.')
param resourceToken string
@description('Owning azd environment name used to isolate alert titles and response plans.')
param environmentName string
@description('Tags applied to all resources.')
param tags object = {}
@description('Log Analytics workspace resource id (alert scope).')
param logAnalyticsWorkspaceId string
@description('PagerDuty "Microsoft Azure" integration URL. Leave empty to skip the PD receiver.')
@secure()
param pagerDutyWebhookUrl string = ''
@description('Keep alert routing to an already-configured action group.')
param pagerDutyConfigured bool = false

var hasPagerDuty = !empty(pagerDutyWebhookUrl)
var routePagerDuty = hasPagerDuty || pagerDutyConfigured
// Resource logs can arrive 3-20 minutes after TimeGenerated. Keep that event-time
// eligibility, but only evaluate rows ingested in the last 20 minutes so the SRE
// incident poller has time to consume the alert before it auto-mitigates.
// Ungrouped summaries emit one zero-valued row when no eligible records remain,
// allowing stateful alerts to resolve instead of treating a healthy window as NoData.
var alertEvaluationFrequency = 'PT5M'
var delayedTelemetryWindow = 'PT30M'
var alertPrefix = 'Zava-${environmentName}'

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (hasPagerDuty) {
  name: 'ag-zava-pagerduty-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'zavaPD'
    enabled: true
    webhookReceivers: hasPagerDuty ? [
      {
        name: 'pagerduty'
        serviceUri: pagerDutyWebhookUrl
        useCommonAlertSchema: true
      }
    ] : []
  }
}

resource quizLaunchFailing 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-quiz-launch-failing'
  location: location
  tags: tags
  properties: {
    description: 'Students are unable to launch quizzes from the portal.'
    severity: 1
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'AzureDiagnostics\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ResourceType == "APPLICATIONGATEWAYS" and Category == "ApplicationGatewayAccessLog"\n| where listenerName_s == "quiz-nsg-listener"\n| extend status = toint(httpStatus_d)\n| where status == 499 or status >= 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource quizContentUnavailable 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-quiz-content-unavailable'
  location: location
  tags: tags
  properties: {
    description: 'Quiz content is unavailable to students.'
    severity: 1
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'AzureDiagnostics\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ResourceType == "APPLICATIONGATEWAYS" and Category == "ApplicationGatewayAccessLog"\n| where listenerName_s == "quiz-app-listener"\n| extend status = toint(httpStatus_d)\n| where status == 404 or status >= 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource quizLaunchErrorsElevated 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-quiz-launch-errors-elevated'
  location: location
  tags: tags
  properties: {
    description: 'Quiz launches consistently return errors for students.'
    severity: 1
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'AzureDiagnostics\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ResourceType == "APPLICATIONGATEWAYS" and Category == "ApplicationGatewayAccessLog"\n| where listenerName_s == "quiz-secret-listener"\n| extend status = toint(httpStatus_d)\n| where status >= 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource portal5xxElevated 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-portal-5xx-elevated'
  location: location
  tags: tags
  properties: {
    description: 'Elevated rate of failed responses from the student portal.'
    severity: 2
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'AzureDiagnostics\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ResourceType == "APPLICATIONGATEWAYS" and Category == "ApplicationGatewayAccessLog"\n| where listenerName_s == "quiz-appgw-listener"\n| extend status = toint(httpStatus_d)\n| where status >= 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 2
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource quizErrorsElevated 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-quiz-errors-elevated'
  location: location
  tags: tags
  properties: {
    description: 'Quiz launches are intermittently failing for students.'
    severity: 2
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'AzureDiagnostics\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ResourceType == "APPLICATIONGATEWAYS" and Category == "ApplicationGatewayAccessLog"\n| where listenerName_s == "quiz-pool-listener"\n| extend status = toint(httpStatus_d)\n| where status >= 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 1
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource quizApiLatencyElevated 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-quiz-api-latency-elevated'
  location: location
  tags: tags
  properties: {
    description: 'Quiz responses are slower than usual for students.'
    severity: 2
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'ContainerAppConsoleLogs_CL\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ContainerAppName_s == "quiz-perf"\n| where Log_s has "ms="\n| extend ms = toint(extract(@"ms=(\\d+)", 1, Log_s))\n| where ms > 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource quizLoadingLatencyElevated 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-quiz-loading-latency-elevated'
  location: location
  tags: tags
  properties: {
    description: 'Quiz loading times are elevated for students.'
    severity: 2
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'ContainerAppConsoleLogs_CL\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ContainerAppName_s == "quiz-query"\n| where Log_s has "ms="\n| extend ms = toint(extract(@"ms=(\\d+)", 1, Log_s))\n| where ms > 500\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

resource gradeExportsFailing 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${alertPrefix}-grade-exports-failing'
  location: location
  tags: tags
  properties: {
    description: 'Zava reporting: nightly grade exports are failing to produce export files.'
    severity: 2
    enabled: true
    evaluationFrequency: alertEvaluationFrequency
    windowSize: delayedTelemetryWindow
    scopes: [ logAnalyticsWorkspaceId ]
    criteria: {
      allOf: [
        {
          query: 'Syslog\n| where TimeGenerated >= ago(30m)\n| where ingestion_time() >= ago(20m)\n| where ProcessName == "zava-export"\n| where SyslogMessage has "FAILED"\n| summarize AggregatedValue = count()'
          metricMeasureColumn: 'AggregatedValue'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    skipQueryValidation: true
    autoMitigate: true
    actions: { actionGroups: routePagerDuty ? [ resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}') ] : [] }
  }
}

output actionGroupId string = resourceId('Microsoft.Insights/actionGroups', 'ag-zava-pagerduty-${resourceToken}')
output actionGroupName string = 'ag-zava-pagerduty-${resourceToken}'
output pagerDutyConfigured bool = routePagerDuty