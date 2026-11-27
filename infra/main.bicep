targetScope = 'resourceGroup'

@description('Dedicated globally unique site name. Use the built-in TLS hostname for the pilot.')
@minLength(3)
@maxLength(50)
param siteName string
param location string = resourceGroup().location
@allowed(['blocked', 'synthetic'])
param edgeState string = 'blocked'
param attachRelay bool = true
@minLength(64)
@maxLength(64)
param gatewaySha string
@minLength(64)
@maxLength(64)
param packageSha string

var suffix = uniqueString(resourceGroup().id, siteName)
var active = edgeState == 'synthetic'
var endpoint = 'knownfaces-mcp.invalid'
var port = 8094

resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: '${siteName}-plan'
  location: location
  kind: 'linux'
  sku: { name: 'B1', capacity: 1 }
  properties: { reserved: true }
}
resource packages 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'kf${suffix}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: packages
  name: 'default'
}
resource releases 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'gateway'
  properties: { publicAccess: 'None' }
}
resource relay 'Microsoft.Relay/namespaces@2024-01-01' = {
  name: 'kf-${suffix}'
  location: location
  sku: { name: 'Standard', tier: 'Standard' }
  properties: {}
}
resource connection 'Microsoft.Relay/namespaces/hybridConnections@2024-01-01' = {
  parent: relay
  name: 'mcp'
  properties: {
    requiresClientAuthorization: true
    userMetadata: '[{"key":"endpoint","value":"knownfaces-mcp.invalid:8094"}]'
  }
}
resource send 'Microsoft.Relay/namespaces/hybridConnections/authorizationRules@2024-01-01' existing = {
  parent: connection
  name: 'edge-send'
}

resource site 'Microsoft.Web/sites@2024-11-01' = {
  name: siteName
  location: location
  kind: 'app,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: active ? 'Enabled' : 'Disabled'
    clientAffinityEnabled: false
  }
}
resource web 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: site
  name: 'web'
  properties: {
    linuxFxVersion: 'NODE|22-lts'
    appCommandLine: 'cd /home/site/wwwroot && test "$(sha256sum gateway.cjs)" = "$KF_GATEWAY_SHA  gateway.cjs" && exec node gateway.cjs'
    alwaysOn: true
    ftpsState: 'Disabled'
    minTlsVersion: '1.2'
    scmMinTlsVersion: '1.2'
    httpLoggingEnabled: false
    detailedErrorLoggingEnabled: false
    requestTracingEnabled: false
    ipSecurityRestrictionsDefaultAction: active ? 'Allow' : 'Deny'
    scmIpSecurityRestrictionsUseMain: false
    scmIpSecurityRestrictionsDefaultAction: 'Deny'
    appSettings: [
      { name: 'KF_EDGE_STATE', value: edgeState }
      { name: 'KF_GATEWAY_SHA', value: gatewaySha }
      { name: 'WEBSITE_RUN_FROM_PACKAGE', value: 'https://${packages.name}.blob.${environment().suffixes.storage}/gateway/${packageSha}.zip' }
      { name: 'WEBSITE_RUN_FROM_PACKAGE_BLOB_MI_RESOURCE_ID', value: 'SystemAssigned' }
      { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'true' }
    ]
  }
}
resource ftp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'ftp'
  properties: { allow: false }
}
resource scm 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'scm'
  properties: { allow: false }
}
resource binding 'Microsoft.Web/sites/hybridConnectionNamespaces/relays@2024-11-01' = if (attachRelay) {
  name: '${site.name}/${relay.name}/${connection.name}'
  properties: {
    hostname: endpoint
    port: port
    relayArmUri: connection.id
    relayName: connection.name
    serviceBusNamespace: relay.name
    serviceBusSuffix: '.servicebus.windows.net'
    sendKeyName: send.name
    sendKeyValue: attachRelay ? send.listKeys().primaryKey : ''
  }
}
