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

variable "github_owner_id" {
  description = "Numeric GitHub ID of the owner. Needed for repositories created after 15 July 2026, whose sign-in tokens carry it. Find it with: gh api users/<owner> --jq .id"
  type        = string
  default     = ""
}

variable "github_repo_id" {
  description = "Numeric GitHub ID of the repository. Find it with: gh api repos/<owner>/<repo> --jq .id"
  type        = string
  default     = ""

  validation {
    condition     = (var.github_repo_id == "") == (var.github_owner_id == "")
    error_message = "Set both github_owner_id and github_repo_id, or neither."
  }
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
