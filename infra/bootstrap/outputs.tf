output "state_bucket" {
  description = "S3 bucket that holds the Terraform state of the main stack."
  value       = aws_s3_bucket.tfstate.bucket
}

output "deploy_role_arn" {
  description = "Role that GitHub Actions assumes on the main branch. Store it as the repository variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.gha_deploy.arn
}

output "plan_role_arn" {
  description = "Read-only role that GitHub Actions assumes on pull requests. Store it as the repository variable AWS_PLAN_ROLE_ARN."
  value       = aws_iam_role.gha_plan.arn
}

output "region" {
  description = "Region the stacks run in."
  value       = var.region
}
