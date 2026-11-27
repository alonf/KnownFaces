targetScope = 'subscription'
param location string
param resourceGroup string
param trustGroup string
resource groups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for name in [resourceGroup, trustGroup]: {
  name: name
  location: location
}]
