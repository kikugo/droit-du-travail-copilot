output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "foundry_name" {
  value = azurerm_cognitive_account.foundry.name
}

output "foundry_endpoint" {
  value = azurerm_cognitive_account.foundry.endpoint
}

output "openai_endpoint" {
  value = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/"
}

output "project_endpoint" {
  value = lookup(azurerm_cognitive_account_project.main.endpoints, "AI Foundry API", null)
}

output "chat_deployment" {
  value = azurerm_cognitive_deployment.chat.name
}

output "embedding_deployment" {
  value = azurerm_cognitive_deployment.embedding.name
}

output "judge_deployment" {
  value = azurerm_cognitive_deployment.judge.name
}

output "search_service_name" {
  value = azurerm_search_service.main.name
}

output "search_endpoint" {
  value = azurerm_search_service.main.endpoint
}

output "app_url" {
  value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}

output "container_app_name" {
  value = azurerm_container_app.api.name
}

# The three GitHub repo variables azure/login needs (none is a secret).
output "ci_client_id" {
  value = azurerm_user_assigned_identity.ci.client_id
}

output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "subscription_id" {
  value = var.subscription_id
}

output "appinsights_connection_string" {
  value     = azurerm_application_insights.main.connection_string
  sensitive = true
}
