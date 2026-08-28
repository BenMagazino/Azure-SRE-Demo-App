# HTTP 500 Error Investigation Runbook

## Trigger Keywords
`500 error`, `internal server error`, `HTTP 500`, `server error`, `application error`, `unresponsive`

## Scope
Azure Container Apps endpoints returning HTTP 500 errors. Logs stored in Log Analytics Workspace.

## Valid Azure Monitor Metric Names for Container Apps
**IMPORTANT: Use ONLY these metric names with `az monitor metrics list`:**
- `UsageNanoCores` — CPU usage (NOT CpuUsage, NOT CPUUsage)
- `WorkingSetBytes` — Memory usage (NOT MemoryUsage, NOT MemoryWorkingSet)
- `Requests` — HTTP request count
- `RestartCount` — Container restarts (OOM indicator)
- `Replicas` — Active replica count
- `CpuPercentage` — CPU percentage
- `MemoryPercentage` — Memory percentage

## Container App Logs CLI
**Use `az containerapp logs show` with `--tail` (NOT `--since`):**
```bash
az containerapp logs show -g <resourceGroup> -n <appName> --tail 300
az containerapp logs show -g <resourceGroup> -n <appName> --tail 300 --format text
```

---

## Phase 1: CPU and Memory Metrics (Check First)

### 1.1 CPU Metrics (App Insights / Azure Monitor)
```kql
performanceCounters
| where timestamp > ago(1h)
| where name == "% Processor Time" or name contains "CPU"
| summarize AvgCPU = avg(value), MaxCPU = max(value) by bin(timestamp, 5m)
| order by timestamp desc
```

### 1.2 Memory Usage Over Time
```kql
performanceCounters
| where timestamp > ago(1h)
| where name contains "Memory" or name == "Available Bytes" or name == "Private Bytes"
| summarize AvgMemory = avg(value), MaxMemory = max(value) by bin(timestamp, 5m), name
| order by timestamp desc
```

### 1.3 Container App Metrics via Azure Monitor
```kql
AzureMetrics
| where TimeGenerated > ago(1h)
| where ResourceProvider == "MICROSOFT.APP"
| where MetricName in ("UsageNanoCores", "WorkingSetBytes", "Requests", "RestartCount")
| summarize AvgValue = avg(Average), MaxValue = max(Maximum) by bin(TimeGenerated, 5m), MetricName
| order by TimeGenerated desc
```

### 1.4 Get Metrics via Azure CLI
```bash
# List available metrics for container app
az monitor metrics list-definitions --resource <resourceId>

# Get CPU usage metrics (last 1 hour)
az monitor metrics list --resource <resourceId> --metric "UsageNanoCores" --interval PT5M --start-time $(date -u -d '1 hour ago' +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -v-1H +"%Y-%m-%dT%H:%M:%SZ") --end-time $(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Get Memory usage metrics (last 1 hour)
az monitor metrics list --resource <resourceId> --metric "WorkingSetBytes" --interval PT5M --start-time $(date -u -d '1 hour ago' +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -v-1H +"%Y-%m-%dT%H:%M:%SZ") --end-time $(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Example with full resource ID:
az monitor metrics list --resource "/subscriptions/cbf44432-7f45-4906-a85d-d2b14a1e8328/resourceGroups/rg-grubify-app/providers/Microsoft.App/containerApps/ca-grubify-api" --metric "UsageNanoCores" --interval PT5M
```

### 1.5 Memory Pressure Indicators
```kql
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| where Log_s contains "OutOfMemory" 
    or Log_s contains "OOM" 
    or Log_s contains "memory pressure"
    or Log_s contains "GC"
    or Log_s contains "heap"
| project TimeGenerated, Log_s, ContainerName_s
| order by TimeGenerated desc
```

### 1.6 Detect High CPU Correlation with Errors
```kql
let highCpuTimes = performanceCounters
| where timestamp > ago(1h)
| where name contains "CPU"
| where value > 80
| summarize by bin(timestamp, 5m);
requests
| where timestamp > ago(1h)
| where resultCode startswith "5"
| summarize ErrorCount = count() by bin(timestamp, 5m)
| join kind=inner highCpuTimes on timestamp
| order by timestamp desc
```

### Resource Thresholds Reference
| Metric | Warning | Critical | Action |
|--------|---------|----------|--------|
| CPU % | > 70% sustained | > 90% sustained | Scale out replicas |
| Memory % | > 75% sustained | > 90% sustained | Scale up memory or fix leak |
| Memory Working Set | Steadily increasing | Near limit | Investigate memory leak |

---

## Phase 2: Initial Triage

### 2.1 Get Container App Details
```bash
# Show container app configuration
az containerapp show -g <resourceGroup> -n <appName> --subscription <subId> --output json
```

### 2.2 Get Current Revision Logs
```bash
# Get recent logs from active revision (last 300 lines)
az containerapp logs show -g <resourceGroup> -n <appName> --subscription <subId> --revision <revisionId> --tail 300

# If command fails, retry with --format text:
az containerapp logs show -g <resourceGroup> -n <appName> --subscription <subId> --revision <revisionId> --tail 300 --format text
```

### 2.3 Check Container App Restart Count / Revision History
```bash
az containerapp revision list -g <resourceGroup> -n <appName> --query "[].{name:name,active:properties.active,restarts:properties.replicas}" -o table
```

---

## Phase 3: Grubify-Specific — Memory Leak in Cart API

This lab intentionally ships a memory leak: `/api/cart/{userId}/items` accumulates cart
items in an in-memory store with no eviction. Rapid repeated calls grow memory until the
container approaches its 1Gi limit, eventually causing OOM restarts and HTTP 500/503s.

1. Confirm `WorkingSetBytes` trending upward with no plateau.
2. Confirm `RestartCount` increasing (OOM kill indicator).
3. Search console logs for repeated POSTs to `/api/cart/*/items`.
4. Root cause: unbounded in-memory cart cache with no TTL/eviction.
5. Remediation options: restart the revision (temporary), add memory-based autoscale, or
   fix the code to evict/expire cart entries.

## Remediation Actions Available to the Agent (autonomous, low access level)

- `az containerapp revision restart` — clears the leaked memory immediately.
- `az containerapp update --min-replicas` — spreads load across more replicas as a stopgap.
- Document root cause and recommended code fix in the incident summary.
