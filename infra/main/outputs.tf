output "ecr_repository_url" {
  description = "Where CI pushes the pipeline container image."
  value       = aws_ecr_repository.app.repository_url
}

output "lake_bucket" {
  description = "Data lake bucket. Pass it to spieltag-ingest with --bucket."
  value       = aws_s3_bucket.lake.bucket
}
