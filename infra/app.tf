# Phase 6: the container app that serves the agent, and the identity GitHub Actions uses.
# Neither holds a key: the app uses a user-assigned identity, CI uses OIDC federation.

# azapi, not azurerm: azurerm 5.11 can't set environmentMode, and an environment created without
# it came up in Express mode, where revisions never provisioned. A standard (workload profiles)
# environment with the serverless Consumption profile scales to zero the same way.
resource "azapi_resource" "environment" {
  type                      = "Microsoft.App/managedEnvironments@2026-07-01"
  name                      = format(local.name, "env")
  parent_id                 = azurerm_resource_group.main.id
  location                  = azurerm_resource_group.main.location
  tags                      = local.tags
  schema_validation_enabled = false # 2026-07-01 is newer than azapi 2.13's embedded schemas

  body = {
    properties = {
      environmentMode = "WorkloadProfiles"
      workloadProfiles = [{
        name                = "Consumption"
        workloadProfileType = "Consumption"
      }]
      appLogsConfiguration = {
        destination = "log-analytics"
        logAnalyticsConfiguration = {
          customerId = azurerm_log_analytics_workspace.main.workspace_id
        }
      }
    }
  }

  sensitive_body = {
    properties = {
      appLogsConfiguration = {
        logAnalyticsConfiguration = {
          sharedKey = azurerm_log_analytics_workspace.main.primary_shared_key
        }
      }
    }
  }

  response_export_values = ["properties.environmentMode"]
}

resource "azurerm_container_app" "api" {
  name                         = format(local.name, "api")
  container_app_environment_id = azapi_resource.environment.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  # Not a key: it only lets the app write telemetry. Kept as a secret so it stays out of logs.
  secret {
    name  = "appinsights-connection-string"
    value = azurerm_application_insights.main.connection_string
  }

  # Container Apps won't pull from a non-ACR registry without a registry entry, even for a
  # public image (Learn: container-apps/github-actions). A read:packages-only PAT.
  secret {
    name  = "ghcr-pull-token"
    value = var.ghcr_pull_token
  }

  registry {
    server               = "ghcr.io"
    username             = var.ghcr_username
    password_secret_name = "ghcr-pull-token"
  }

  ingress {
    external_enabled = true
    target_port      = 8000

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    # One replica keeps the in-memory rate limits in copilot/limits.py exact.
    max_replicas = 1

    container {
      name   = "api"
      image  = "${var.image}:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      # DefaultAzureCredential picks the user-assigned identity by its client id.
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
      env {
        name  = "FOUNDRY_PROJECT_ENDPOINT"
        value = lookup(azurerm_cognitive_account_project.main.endpoints, "AI Foundry API", null)
      }
      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/"
      }
      env {
        name  = "CHAT_DEPLOYMENT"
        value = azurerm_cognitive_deployment.chat.name
      }
      env {
        name  = "EMBEDDING_DEPLOYMENT"
        value = azurerm_cognitive_deployment.embedding.name
      }
      env {
        name  = "AZURE_SEARCH_ENDPOINT"
        value = azurerm_search_service.main.endpoint
      }
      env {
        name  = "SEARCH_INDEX"
        value = var.search_index
      }
      env {
        name  = "DAILY_QUESTION_CAP"
        value = tostring(var.daily_question_cap)
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-connection-string"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
    }
  }

  # CI rolls new images with `az containerapp update`; Terraform owns everything else.
  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }
}

resource "azurerm_user_assigned_identity" "app" {
  name                = format(local.name, "app")
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# GitHub Actions identity. A user-assigned identity with federated credentials gives keyless
# OIDC without an Entra app registration.
resource "azurerm_user_assigned_identity" "ci" {
  name                = format(local.name, "ci")
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

locals {
  # GitHub OIDC token subjects allowed to sign in as the CI identity.
  ci_subjects = {
    pull-request = "repo:${var.github_oidc_repo}:pull_request"
    main         = "repo:${var.github_oidc_repo}:ref:refs/heads/main"
  }
}

resource "azurerm_federated_identity_credential" "ci" {
  for_each = local.ci_subjects

  name                      = "github-${each.key}"
  user_assigned_identity_id = azurerm_user_assigned_identity.ci.id
  issuer                    = "https://token.actions.githubusercontent.com"
  audience                  = ["api://AzureADTokenExchange"]
  subject                   = each.value
}

locals {
  role_ids = {
    foundry_user             = "53ca6127-db72-4b80-b1b0-d745d6d5456d" # Foundry User
    search_index_data_reader = "1407120a-92aa-4202-b7e9-c0e197c71c8f" # Search Index Data Reader
    container_apps_contrib   = "358470bc-b998-42bd-ab17-a7e34c199c0f" # Container Apps Contributor
  }

  # principal, scope and role for each assignment; read-only on Search for both.
  workload_roles = {
    app_foundry = {
      principal_id = azurerm_user_assigned_identity.app.principal_id
      scope        = azurerm_cognitive_account.foundry.id
      role_id      = local.role_ids.foundry_user
    }
    app_search = {
      principal_id = azurerm_user_assigned_identity.app.principal_id
      scope        = azurerm_search_service.main.id
      role_id      = local.role_ids.search_index_data_reader
    }
    ci_foundry = {
      principal_id = azurerm_user_assigned_identity.ci.principal_id
      scope        = azurerm_cognitive_account.foundry.id
      role_id      = local.role_ids.foundry_user
    }
    ci_search = {
      principal_id = azurerm_user_assigned_identity.ci.principal_id
      scope        = azurerm_search_service.main.id
      role_id      = local.role_ids.search_index_data_reader
    }
    # Scoped to the one container app, so CI can roll an image and nothing else.
    ci_deploy = {
      principal_id = azurerm_user_assigned_identity.ci.principal_id
      scope        = azurerm_container_app.api.id
      role_id      = local.role_ids.container_apps_contrib
    }
  }
}

resource "azurerm_role_assignment" "workload" {
  for_each = local.workload_roles

  scope              = each.value.scope
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value.role_id}"
  principal_id       = each.value.principal_id
  principal_type     = "ServicePrincipal"
}
