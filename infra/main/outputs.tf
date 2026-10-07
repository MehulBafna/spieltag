output "ecr_repository_url" {
  description = "Where CI pushes the pipeline container image."
  value       = aws_ecr_repository.app.repository_url
}

output "lake_bucket" {
  description = "Data lake bucket. Pass it to spieltag-ingest with --bucket."
  value       = aws_s3_bucket.lake.bucket
}

output "site_url" {
  description = "The public dashboard."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "cloudfront_distribution_id" {
  description = "Used by CI to clear the cache after a deploy."
  value       = aws_cloudfront_distribution.site.id
}

output "lambda_function_name" {
  description = "The scheduled pipeline function."
  value       = aws_lambda_function.pipeline.function_name
}

output "alerts_topic_arn" {
  description = "Alarms publish here."
  value       = aws_sns_topic.alerts.arn
}
