# Durable daily question counter for copilot/limits.py: one tiny blob per UTC day, so the
# 300-a-day cap survives scale-to-zero and new revisions. Pennies a month (LRS, a few KB).
# Keyless: shared keys are off and the app writes with its managed identity.
resource "azurerm_storage_account" "counter" {
  name                            = "${var.prefix}st${random_string.suffix.result}"
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_container" "counters" {
  name                  = "counters"
  storage_account_id    = azurerm_storage_account.counter.id
  container_access_type = "private"
}

# Scoped to the one container, not the account.
resource "azurerm_role_assignment" "app_counter" {
  scope                = azurerm_storage_container.counters.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}
