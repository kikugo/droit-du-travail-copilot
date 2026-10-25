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

output "appinsights_connection_string" {
  value     = azurerm_application_insights.main.connection_string
  sensitive = true
}
