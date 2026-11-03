variable "subscription_id" {
  description = "Azure subscription to deploy into."
  type        = string
}

variable "resource_group_name" {
  description = "Existing resource group from infra/ (terraform -chdir=infra output resource_group_name)."
  type        = string
}

variable "location" {
  description = "Region for the benchmark search service."
  type        = string
  default     = "polandcentral"
}

variable "name" {
  description = "Search service name (globally unique)."
  type        = string
}

variable "foundry_name" {
  description = "Existing Foundry account (terraform -chdir=infra output foundry_name)."
  type        = string
}
