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
