# Alarms. They email the address in var.alert_email. A pipeline that fails quietly is worse than
# one that fails loudly, so every alarm below maps to a way the pipeline can stop being honest.

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
  league_dim    = { League = var.league }
}

# The function crashed or could not write forecasts.
resource "aws_cloudwatch_metric_alarm" "function_errors" {
  alarm_name          = "${var.project}-function-errors"
  alarm_description   = "The pipeline function failed. Look at its logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.pipeline.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# No successful run for three hours: the schedule, the function or the account is broken.
resource "aws_cloudwatch_metric_alarm" "heartbeat" {
  alarm_name          = "${var.project}-no-recent-run"
  alarm_description   = "The pipeline has not completed a run in three hours."
  namespace           = "Spieltag"
  metric_name         = "CycleCompleted"
  dimensions          = local.league_dim
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# A match kicked off in the last three days without a frozen forecast.
resource "aws_cloudwatch_metric_alarm" "missed_forecasts" {
  alarm_name          = "${var.project}-missed-forecast"
  alarm_description   = "A match kicked off without a frozen forecast. It will never be scored."
  namespace           = "Spieltag"
  metric_name         = "MissedFreezes"
  dimensions          = local.league_dim
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# The match data feed failed six hours in a row. Forecasts continue from stored data, but stale.
resource "aws_cloudwatch_metric_alarm" "ingest_failing" {
  alarm_name          = "${var.project}-ingest-failing"
  alarm_description   = "Fetching or validating match data has failed for six hours."
  namespace           = "Spieltag"
  metric_name         = "IngestFailed"
  dimensions          = local.league_dim
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# A stored forecast is unreadable or was changed after the fact.
resource "aws_cloudwatch_metric_alarm" "problem_records" {
  alarm_name          = "${var.project}-problem-records"
  alarm_description   = "A frozen forecast failed its integrity check or the data has an inconsistency."
  namespace           = "Spieltag"
  metric_name         = "ProblemRecords"
  dimensions          = local.league_dim
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# A sealed match is in play but the live feed has failed three ticks in a row (15 minutes). The
# sealed forecasts are not affected; only the live line on the dashboard is stale.
resource "aws_cloudwatch_metric_alarm" "inplay_feed_failing" {
  alarm_name          = "${var.project}-inplay-feed-failing"
  alarm_description   = "The live match feed failed for 15 minutes during a match. The live line is stale."
  namespace           = "Spieltag"
  metric_name         = "InplayFeedFailed"
  dimensions          = local.league_dim
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
