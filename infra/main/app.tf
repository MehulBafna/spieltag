# The pipeline: one container image run by Lambda, started every hour by EventBridge Scheduler.

locals {
  function_name = "${var.project}-pipeline"
  lake_arn      = aws_s3_bucket.lake.arn
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# What the function may do: read the lake, write only the places it owns, never delete.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "pipeline" {
  statement {
    sid     = "ReadTheLake"
    actions = ["s3:GetObject"]

    resources = [
      "${local.lake_arn}/raw/*",
      "${local.lake_arn}/curated/*",
      "${local.lake_arn}/predictions/*",
      "${local.lake_arn}/meta/*",
      "${local.lake_arn}/evaluation/*",
      "${local.lake_arn}/site/data/*",
    ]
  }

  statement {
    sid     = "WriteWhatThePipelineOwns"
    actions = ["s3:PutObject"]

    resources = [
      "${local.lake_arn}/raw/*",
      "${local.lake_arn}/curated/*",
      "${local.lake_arn}/predictions/*",
      "${local.lake_arn}/meta/*",
      "${local.lake_arn}/site/data/*",
    ]
  }

  # Without this, reading an object that does not exist answers "access denied" instead of
  # "not found", and the pipeline could not tell a first run from a permission problem.
  statement {
    sid       = "ListTheLake"
    actions   = ["s3:ListBucket"]
    resources = [local.lake_arn]
  }

  statement {
    sid     = "WriteLogs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]

    resources = ["${aws_cloudwatch_log_group.pipeline.arn}:*"]
  }
}

resource "aws_iam_role" "pipeline" {
  name               = "${var.project}-app-pipeline"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "pipeline" {
  name   = "pipeline"
  role   = aws_iam_role.pipeline.id
  policy = data.aws_iam_policy_document.pipeline.json
}

# ---------------------------------------------------------------------------
# The function
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "pipeline" {
  function_name = local.function_name
  role          = aws_iam_role.pipeline.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_seconds

  environment {
    variables = {
      SPIELTAG_BUCKET       = aws_s3_bucket.lake.bucket
      SPIELTAG_LEAGUE       = var.league
      SPIELTAG_CODE_VERSION = var.image_tag
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.pipeline,
    aws_iam_role_policy.pipeline,
    aws_ecr_repository_policy.app,
  ]
}

# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.pipeline.arn]
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-app-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "invoke-pipeline"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

resource "aws_scheduler_schedule" "pipeline" {
  name                = "${var.project}-pipeline-hourly"
  description         = "Ingest, freeze due forecasts, score, publish."
  schedule_expression = var.schedule_expression
  state               = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.pipeline.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "schedule" })

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
