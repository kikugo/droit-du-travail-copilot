# One Basic search service for the four-mode benchmark on the full corpus, with the semantic
# ranker on its free plan (1,000 queries a month). Basic bills by the hour: apply, index,
# run eval.run_retrieval --label basic, then destroy the same day. Separate state from infra/.
data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

resource "azurerm_search_service" "benchmark" {
  name                = var.name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.main.name
  sku                 = "basic"
  semantic_search_sku = "free"

  local_authentication_enabled = true
  authentication_failure_mode  = "http401WithBearerChallenge"

  tags = {
    project = "droit-du-travail-copilot"
    purpose = "one-day retrieval benchmark"
  }
}

resource "azurerm_role_assignment" "current_user" {
  for_each = {
    search_service_contributor    = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
    search_index_data_contributor = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
  }

  scope              = azurerm_search_service.benchmark.id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value}"
  principal_id       = data.azurerm_client_config.current.object_id
}
