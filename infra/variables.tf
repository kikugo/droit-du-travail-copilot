locals {
  # Regions the Azure for Students policy lets this subscription deploy to.
  allowed_regions = ["swedencentral", "germanywestcentral", "polandcentral", "spaincentral", "uaenorth"]
}

variable "subscription_id" {
  description = "Azure subscription to deploy into."
  type        = string
}

variable "prefix" {
  description = "Short prefix used in every resource name."
  type        = string
  default     = "ddt"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,7}$", var.prefix))
    error_message = "prefix must be 2-8 lowercase letters or digits, starting with a letter."
  }
}

variable "location" {
  description = "Region for the resource group, Foundry account and monitoring."
  type        = string
  default     = "swedencentral"

  validation {
    condition     = contains(local.allowed_regions, var.location)
    error_message = "location must be one of the regions allowed by the subscription policy."
  }
}

variable "search_location" {
  description = "Region for Azure AI Search. Sweden and Germany are capacity-blocked for new search services."
  type        = string
  default     = "polandcentral"

  validation {
    condition     = contains(local.allowed_regions, var.search_location)
    error_message = "search_location must be one of the regions allowed by the subscription policy."
  }
}

variable "chat_model_name" {
  description = "Chat model to deploy."
  type        = string
  default     = "gpt-5.4-mini"
}

variable "chat_model_version" {
  description = "Chat model version, pinned so eval runs stay comparable."
  type        = string
  default     = "2026-03-17"
}

variable "chat_capacity" {
  description = "Chat deployment rate limit, in thousands of tokens per minute."
  type        = number
  default     = 50
}

variable "judge_model_name" {
  description = "Judge model for faithfulness (eval only)."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "judge_model_version" {
  description = "Judge model version, pinned so faithfulness scores stay comparable."
  type        = string
  default     = "2025-04-14"
}

variable "judge_capacity" {
  description = "Judge deployment rate limit, in thousands of tokens per minute."
  type        = number
  default     = 50
}

variable "embedding_capacity" {
  description = "Embedding deployment rate limit, in thousands of tokens per minute."
  type        = number
  default     = 100
}

variable "log_daily_quota_gb" {
  description = "Daily ingestion cap for Log Analytics and Application Insights."
  type        = number
  default     = 0.15
}

variable "image" {
  description = "Container image for the app, without the tag (public GHCR package)."
  type        = string
  default     = "ghcr.io/kikugo/droit-du-travail-copilot"
}

variable "ghcr_username" {
  description = "GitHub user that owns the GHCR package."
  type        = string
  default     = "kikugo"
}

variable "ghcr_pull_token" {
  description = "Classic GitHub PAT with only read:packages, for Container Apps to pull the image. Set it in terraform.tfvars (gitignored)."
  type        = string
  sensitive   = true
}

variable "image_tag" {
  description = "Tag for the first deploy; CI rolls later tags with az containerapp update."
  type        = string
  default     = "latest"
}

variable "search_index" {
  description = "Index the app queries (built in Phase 3)."
  type        = string
  default     = "articles-v1"
}

variable "daily_question_cap" {
  description = "Questions /ask answers per UTC day before returning 429 (spend guard)."
  type        = number
  default     = 300
}

variable "github_oidc_repo" {
  description = "The repo part of GitHub's OIDC subject claim. GitHub now sends owner@owner_id/name@repo_id (gh api repos/OWNER/NAME --jq '.owner.id, .id')."
  type        = string
  default     = "kikugo@83645138/droit-du-travail-copilot@1432202888"
}

variable "grant_current_user_access" {
  description = "Give the identity running terraform data-plane roles on Foundry and Search."
  type        = bool
  default     = true
}
