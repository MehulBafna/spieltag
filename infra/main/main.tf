# Main stack: the container registry and the data lake bucket (this file), the scheduled Lambda
# (app.tf), the public dashboard (site.tf) and the alarms (alarms.tf).

data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  lake_bucket = "${var.project}-lake-${local.account_id}"
}

# ---------------------------------------------------------------------------
# Container registry for the Lambda image
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = "${var.project}-pipeline"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 3 most recent images"

        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 3
        }

        action = {
          type = "expire"
        }
      }
    ]
  })
}

# Lambda pulls the image with its own service permissions. This limits that to this project's
# functions.
data "aws_iam_policy_document" "ecr_pull" {
  statement {
    sid = "LambdaCanPullThisProjectsImages"

    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:sourceArn"
      values   = ["arn:aws:lambda:${var.region}:${local.account_id}:function:${var.project}-*"]
    }
  }
}

resource "aws_ecr_repository_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy     = data.aws_iam_policy_document.ecr_pull.json
}

# ---------------------------------------------------------------------------
# Data lake bucket: raw/, curated/, features/, predictions/, models/ and site/
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "lake" {
  bucket = local.lake_bucket
}

resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket = aws_s3_bucket.lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.keep_noncurrent_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # The dashboard's JSON files are rewritten every hour. Keep their old versions only briefly.
  rule {
    id     = "expire-old-dashboard-data"
    status = "Enabled"

    filter {
      prefix = "site/data/"
    }

    noncurrent_version_expiration {
      noncurrent_days = 3
    }
  }

  depends_on = [aws_s3_bucket_versioning.lake]
}

data "aws_iam_policy_document" "lake" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.lake.arn,
      "${aws_s3_bucket.lake.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # A frozen forecast is final. Every write under predictions/ must be a conditional write that
  # only succeeds if the object does not exist yet, so nobody, the pipeline included, can
  # replace one. Changing this takes an explicit change to the bucket policy.
  statement {
    sid    = "PredictionsAreWriteOnce"
    effect = "Deny"

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.lake.arn}/predictions/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Null"
      variable = "s3:if-none-match"
      values   = ["true"]
    }
  }

  statement {
    sid    = "PredictionsCannotBeDeleted"
    effect = "Deny"

    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.lake.arn}/predictions/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }

  # The dashboard is served from site/ only. Everything else in the bucket stays private.
  statement {
    sid     = "CloudFrontReadsTheSite"
    actions = ["s3:GetObject"]

    resources = ["${aws_s3_bucket.lake.arn}/site/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "lake" {
  bucket = aws_s3_bucket.lake.id
  policy = data.aws_iam_policy_document.lake.json

  depends_on = [aws_s3_bucket_public_access_block.lake]
}
