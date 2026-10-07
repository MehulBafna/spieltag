variable "project" {
  description = "Name prefix for everything this stack creates."
  type        = string
  default     = "spieltag"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-central-1"
}

variable "github_owner" {
  description = "GitHub user or organisation that owns the repository."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name, without the owner."
  type        = string
}

variable "alert_email" {
  description = "Email address that receives the budget alerts."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly cost limit in USD. Alerts fire at 80 percent of actual and 100 percent of forecast spend."
  type        = number
  default     = 5
}

variable "create_github_oidc_provider" {
  description = "Set to false if the account already has the GitHub Actions OIDC provider. Only one is allowed per account."
  type        = bool
  default     = true
}
