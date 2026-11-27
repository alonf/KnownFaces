targetScope = 'resourceGroup'
param siteName string
param applyPrincipal string
param publisherPrincipal string
param homeOperator string
param sitePrincipal string
var group = resourceGroup().id
var suffix = uniqueString(group, siteName)
var keyRole = extensionResourceId(group, 'Microsoft.Authorization/roleDefinitions', guid(group, 'knownfaces-role', 'relay-key'))
var blobWriter = extensionResourceId(group, 'Microsoft.Authorization/roleDefinitions', guid(group, 'knownfaces-role', 'publish-blob'))
var blobReader = subscriptionResourceId('Microsoft.Authorization/roleDefinitions','2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
resource relay 'Microsoft.Relay/namespaces@2024-01-01' existing = { name: 'kf-${suffix}' }
resource connection 'Microsoft.Relay/namespaces/hybridConnections@2024-01-01' existing = { parent: relay, name: 'mcp' }
resource sender 'Microsoft.Relay/namespaces/hybridConnections/authorizationRules@2024-01-01' = {
  parent: connection
  name: 'edge-send'
  properties: { rights: ['Send'] }
}
resource listener 'Microsoft.Relay/namespaces/hybridConnections/authorizationRules@2024-01-01' = {
  parent: connection
  name: 'home-listen'
  properties: { rights: ['Listen'] }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: 'kf${suffix}' }
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = { parent: storage, name: 'default' }
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = { parent: blobs, name: 'gateway' }
resource sendAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sender.id, applyPrincipal, keyRole)
  scope: sender
  properties: { principalId: applyPrincipal, principalType: 'ServicePrincipal', roleDefinitionId: keyRole }
}
resource listenAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(listener.id, homeOperator, keyRole)
  scope: listener
  properties: { principalId: homeOperator, principalType: 'User', roleDefinitionId: keyRole }
}
resource readers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in [applyPrincipal,sitePrincipal]: {
  name: guid(container.id, principal, blobReader)
  scope: container
  properties: { principalId: principal, principalType: 'ServicePrincipal', roleDefinitionId: blobReader }
}]
resource writer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: container
  name: guid(container.id, publisherPrincipal, blobWriter)
  properties: { principalId: publisherPrincipal, principalType: 'ServicePrincipal', roleDefinitionId: blobWriter }
}
