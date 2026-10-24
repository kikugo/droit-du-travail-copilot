data "azurerm_client_config" "current" {}

# Foundry subdomains and search service names are global, so add a suffix.
resource "random_string" "suffix" {
  length  = 5
  lower   = true
  upper   = false
  numeric = true
  special = false
}

locals {
  name = "${var.prefix}-%s-${random_string.suffix.result}"
  tags = {
    project = "droit-du-travail-copilot"
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${var.prefix}-${random_string.suffix.result}"
  location = var.location
  tags     = local.tags
}

# Foundry resource: hosts the model deployments and the project.
resource "azurerm_cognitive_account" "foundry" {
  name                       = format(local.name, "foundry")
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  kind                       = "AIServices"
  sku_name                   = "S0"
  custom_subdomain_name      = format(local.name, "foundry")
  project_management_enabled = true
  local_auth_enabled         = true
  tags                       = local.tags

  # A project can only be created when the account has a managed identity.
  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_account_project" "main" {
  name                 = "${var.prefix}-copilot"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  location             = azurerm_resource_group.main.location
  display_name         = "Droit du Travail Copilot"
  description          = "French labour-law agent"
  tags                 = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_deployment" "chat" {
  name                   = var.chat_model_name
  cognitive_account_id   = azurerm_cognitive_account.foundry.id
  version_upgrade_option = "NoAutoUpgrade"

  model {
    format  = "OpenAI"
    name    = var.chat_model_name
    version = var.chat_model_version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.chat_capacity
  }
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                   = "text-embedding-3-small"
  cognitive_account_id   = azurerm_cognitive_account.foundry.id
  version_upgrade_option = "NoAutoUpgrade"

  model {
    format  = "OpenAI"
    name    = "text-embedding-3-small"
    version = "1"
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.embedding_capacity
  }

  # Azure rejects concurrent deployment changes on the same account.
  depends_on = [azurerm_cognitive_deployment.chat]
}

resource "azurerm_search_service" "main" {
  name                = format(local.name, "search")
  location            = var.search_location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "free"

  # Accept both API keys and Entra ID tokens.
  local_authentication_enabled = true
  authentication_failure_mode  = "http401WithBearerChallenge"

  tags = local.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = format(local.name, "logs")
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = local.tags
}

resource "azurerm_application_insights" "main" {
  name                 = format(local.name, "appi")
  location             = azurerm_resource_group.main.location
  resource_group_name  = azurerm_resource_group.main.name
  workspace_id         = azurerm_log_analytics_workspace.main.id
  application_type     = "web"
  daily_data_cap_in_gb = var.log_daily_quota_gb
  tags                 = local.tags
}

# Data-plane roles for whoever runs terraform, so local scripts need no keys.
# Role IDs instead of names because the Foundry roles were renamed recently.
locals {
  user_roles = var.grant_current_user_access ? {
    foundry_user = {
      scope   = azurerm_cognitive_account.foundry.id
      role_id = "53ca6127-db72-4b80-b1b0-d745d6d5456d" # Foundry User
    }
    search_service_contributor = {
      scope   = azurerm_search_service.main.id
      role_id = "7ca78c08-252a-4471-8644-bb5ff32d4ba0" # Search Service Contributor
    }
    search_index_data_contributor = {
      scope   = azurerm_search_service.main.id
      role_id = "8ebe5a00-799e-43f5-93ac-243d3dce84a7" # Search Index Data Contributor
    }
  } : {}
}

resource "azurerm_role_assignment" "current_user" {
  for_each = local.user_roles

  scope              = each.value.scope
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value.role_id}"
  principal_id       = data.azurerm_client_config.current.object_id
}
