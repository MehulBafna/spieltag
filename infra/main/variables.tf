variable "project" {
  description = "Name prefix for everything this stack creates. Keep it the same as in the bootstrap stack, the deploy role is limited to names that start with it."
  type        = string
  default     = "spieltag"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-central-1"
}

variable "keep_noncurrent_days" {
  description = "How long old versions of data lake files are kept after they are replaced."
  type        = number
  default     = 180
}

variable "alert_email" {
  description = "Email address that receives pipeline alarms. AWS sends a confirmation link to it once."
  type        = string
}

variable "image_tag" {
  description = "Tag of the pipeline image in ECR that the Lambda function runs. CI passes the git commit hash."
  type        = string
}

variable "league" {
  description = "OpenLigaDB league shortcut the pipeline forecasts."
  type        = string
  default     = "bl1"
}

variable "schedule_expression" {
  description = "When the pipeline runs (UTC). The default is five past every hour."
  type        = string
  default     = "cron(5 * * * ? *)"
}

variable "lambda_memory_mb" {
  description = "Memory of the pipeline function. More memory also means more CPU."
  type        = number
  default     = 1536
}

variable "lambda_timeout_seconds" {
  description = "Time limit of one pipeline run."
  type        = number
  default     = 300
}

variable "log_retention_days" {
  description = "How long the pipeline's logs are kept."
  type        = number
  default     = 30
}
