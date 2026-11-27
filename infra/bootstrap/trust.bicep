targetScope = 'resourceGroup'

@description('owner/repository of the source release')
param repository string
param prefix string
var environments = ['preview', 'apply', 'publish']

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for env in environments: {
  name: '${prefix}-${env}'
  location: resourceGroup().location
}]
resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [for (env, i) in environments: {
  parent: identities[i]
  name: 'github-environment'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    audiences: ['api://AzureADTokenExchange']
    subject: 'repo:${repository}:environment:knownfaces-${env}'
  }
}]
output principals array = [for (env, i) in environments: {
  environment: env
  principal: identities[i].properties.principalId
  client: identities[i].properties.clientId
}]
