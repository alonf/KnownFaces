targetScope = 'resourceGroup'
param principals array
var group = resourceGroup().id
var reads = [
  'Microsoft.Resources/subscriptions/resourceGroups/read'
  'Microsoft.Resources/deployments/read'
  'Microsoft.Resources/deployments/operations/read'
  'Microsoft.Resources/deployments/operationStatuses/read'
  'Microsoft.Resources/deployments/validate/action'
  'Microsoft.Resources/deployments/whatIf/action'
  'Microsoft.Web/*/read'
  'Microsoft.Relay/*/read'
  'Microsoft.Storage/storageAccounts/read'
  'Microsoft.Storage/storageAccounts/blobServices/read'
  'Microsoft.Storage/storageAccounts/blobServices/containers/read'
]
var writes = [
  'Microsoft.Resources/deployments/write'
  'Microsoft.Web/serverfarms/write'
  'Microsoft.Web/serverfarms/join/action'
  'Microsoft.Web/sites/write'
  'Microsoft.Web/sites/config/write'
  'Microsoft.Web/sites/basicPublishingCredentialsPolicies/write'
  'Microsoft.Web/sites/hybridConnectionNamespaces/relays/write'
  'Microsoft.Relay/namespaces/write'
  'Microsoft.Relay/namespaces/hybridConnections/write'
  'Microsoft.Storage/storageAccounts/write'
  'Microsoft.Storage/storageAccounts/blobServices/containers/write'
]
var definitions = [
  { name: 'preview', actions: reads, dataActions: [] }
  { name: 'apply', actions: concat(reads, writes), dataActions: [] }
  { name: 'relay-key', actions: ['Microsoft.Relay/namespaces/hybridConnections/authorizationRules/listKeys/action'], dataActions: [] }
  { name: 'publish-blob', actions: ['Microsoft.Storage/storageAccounts/blobServices/containers/read'], dataActions: ['Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read','Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'] }
]
resource roles 'Microsoft.Authorization/roleDefinitions@2022-04-01' = [for role in definitions: {
  name: guid(group, 'knownfaces-role', role.name)
  properties: {
    roleName: 'KnownFaces ${role.name} ${uniqueString(group)}'
    description: 'Dedicated KnownFaces deployment role'
    type: 'CustomRole'
    assignableScopes: [group]
    permissions: [{ actions: role.actions, notActions: [], dataActions: role.dataActions, notDataActions: [] }]
  }
}]
resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in principals: {
  name: guid(group, principal.principal, principal.environment)
  properties: {
    principalId: principal.principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles[principal.environment == 'apply' ? 1 : 0].id
  }
}]
