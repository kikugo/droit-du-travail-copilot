provider "azurerm" {
  features {}

  subscription_id = var.subscription_id

  # azurerm 5.x registers nothing by default, so list only what this config uses.
  resource_providers_to_register = [
    "Microsoft.CognitiveServices",
    "Microsoft.Search",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.App",
    "Microsoft.ManagedIdentity",
    "Microsoft.Storage",
  ]
}

provider "azapi" {
  subscription_id = var.subscription_id
}
