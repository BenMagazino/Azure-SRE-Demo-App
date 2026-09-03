// Key Vault holding the platform's database credentials. RBAC-authorized; the container
// apps' user-assigned identity is granted Key Vault Secrets User so Container Apps can
// resolve secret references at runtime.
//
// Secrets:
//   * db-password           — the real admin password, used by the baseline + most lanes.
//   * db-password-secretlane — a COPY of the password, used ONLY by the secret lane.
//                              chaos/break-secret.ps1 rotates this to an invalid value so
//                              only that lane hits authentication failures.
//   * db-pool-password      — login for the dedicated app_pool role (pool lane), created
//                              by post-provision; chaos/break-pool.ps1 sets a real
//                              CONNECTION LIMIT on that role.
@description('Azure region.')
param location string
@description('Resource name suffix token.')
param resourceToken string
@description('Tags applied to all resources.')
param tags object = {}

@description('Principal id of the container apps user-assigned identity (granted Secrets User).')
param identityPrincipalId string

@description('Principal id of the reporting VM system-assigned identity (granted Secrets Officer for private-network operations).')
param vmPrincipalId string

@description('Resource id of the subnet that hosts the Key Vault private endpoint.')
param privateEndpointSubnetId string

@description('Resource id of the VNet linked to the Key Vault private DNS zone.')
param vnetId string

@secure()
@description('PostgreSQL admin password (stored as db-password and db-password-secretlane).')
param dbAdminPassword string
@secure()
@description('Password for the dedicated app_pool role (pool lane).')
param dbPoolPassword string
@secure()
@description('Admin password for the reporting-worker VM.')
param vmAdminPassword string

var vaultName = 'kv-zava-${resourceToken}'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: tenant().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
    }
  }
}

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}

resource privateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: privateDnsZone
  name: 'zava-vnet-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${vaultName}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: vault.id
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'key-vault'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
}

resource secretDbPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'db-password'
  properties: { value: dbAdminPassword }
}

resource secretSecretLane 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'db-password-secretlane'
  properties: { value: dbAdminPassword }
}

resource secretPoolPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'db-pool-password'
  properties: { value: dbPoolPassword }
}

resource secretVmPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'vm-admin-password'
  properties: { value: vmAdminPassword }
}

// Key Vault Secrets User -> the apps' managed identity.
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identityPrincipalId, secretsUserRoleId)
  scope: vault
  properties: {
    principalId: identityPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

// The VM is the controlled bridge for Key Vault data-plane operations because the vault
// accepts traffic only from its private endpoint.
var secretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
resource vmSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, vmPrincipalId, secretsOfficerRoleId)
  scope: vault
  properties: {
    principalId: vmPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsOfficerRoleId)
    principalType: 'ServicePrincipal'
  }
}

output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output privateEndpointName string = privateEndpoint.name
output dbCredentialUri string = '${vault.properties.vaultUri}secrets/db-password'
output secretUriSecretLane string = '${vault.properties.vaultUri}secrets/db-password-secretlane'
output poolCredentialUri string = '${vault.properties.vaultUri}secrets/db-pool-password'
