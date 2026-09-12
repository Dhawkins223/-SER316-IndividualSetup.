# Alarms and budgets.
#
# Every alarm here is one someone would act on. Alarms that fire routinely and
# are routinely ignored are worse than no alarms: they train the operator to
# dismiss the channel, and then the one that matters is dismissed too.
#
# The RDS free-storage alarm is the reason this module exists. The Railway
# incident was a full volume with no warning ahead of it, and RDS storage
# autoscaling plus this alarm are the two independent guards against a repeat.

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alert_email_addresses)

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name        = "${var.name_prefix}-rds-free-storage"
  alarm_description = "RDS free storage below ${var.rds_free_storage_threshold_gb} GB. This is the alarm the Railway full-volume incident did not have; act before autoscaling is the only thing left."

  namespace   = "AWS/RDS"
  metric_name = "FreeStorageSpace"
  statistic   = "Average"
  period      = 300

  comparison_operator = "LessThanThreshold"
  threshold           = var.rds_free_storage_threshold_gb * 1024 * 1024 * 1024
  evaluation_periods  = 2

  dimensions = { DBInstanceIdentifier = var.rds_instance_id }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  # Missing data on a storage metric means the instance is not reporting, which
  # is itself worth knowing rather than treating as healthy.
  treat_missing_data = "breaching"

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name        = "${var.name_prefix}-rds-cpu"
  alarm_description = "RDS CPU sustained above ${var.rds_cpu_threshold}%."

  namespace   = "AWS/RDS"
  metric_name = "CPUUtilization"
  statistic   = "Average"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.rds_cpu_threshold
  evaluation_periods  = 3

  dimensions = { DBInstanceIdentifier = var.rds_instance_id }

  alarm_actions      = local.alarm_actions
  ok_actions         = local.alarm_actions
  treat_missing_data = "notBreaching"

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name        = "${var.name_prefix}-rds-connections"
  alarm_description = "RDS connection count above ${var.rds_connection_threshold}. The pool is bounded by DATABASE_POOL_MAX_SIZE, so this usually means tasks are not shutting down cleanly."

  namespace   = "AWS/RDS"
  metric_name = "DatabaseConnections"
  statistic   = "Maximum"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.rds_connection_threshold
  evaluation_periods  = 2

  dimensions = { DBInstanceIdentifier = var.rds_instance_id }

  alarm_actions      = local.alarm_actions
  treat_missing_data = "notBreaching"

  tags = var.tags
}

# --------------------------------------------------------------------------
# Load balancer and web service
# --------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_hosts" {
  alarm_name        = "${var.name_prefix}-alb-unhealthy-hosts"
  alarm_description = "One or more web targets are failing their health check."

  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  statistic   = "Maximum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 3

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }

  alarm_actions      = local.alarm_actions
  ok_actions         = local.alarm_actions
  treat_missing_data = "notBreaching"

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name        = "${var.name_prefix}-alb-5xx"
  alarm_description = "Application 5xx responses above ${var.alb_5xx_threshold} in five minutes."

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_5XX_Count"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alb_5xx_threshold
  evaluation_periods  = 1

  dimensions = { LoadBalancer = var.alb_arn_suffix }

  alarm_actions = local.alarm_actions
  # A period with no requests reports no datapoint. Treating that as breaching
  # would alarm every quiet night.
  treat_missing_data = "notBreaching"

  tags = var.tags
}

# --------------------------------------------------------------------------
# Scheduled workers
#
# A scheduled task that fails to *start* is invisible without this: no task
# means no application logs to notice the absence in.
# --------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "scheduler_failures" {
  count = var.enable_scheduler_alarm ? 1 : 0

  alarm_name        = "${var.name_prefix}-scheduler-invocation-failures"
  alarm_description = "EventBridge Scheduler could not start a scheduled worker task."

  namespace   = "AWS/Scheduler"
  metric_name = "InvocationAttemptsFailedToBeSentToDeadLetterCount"
  statistic   = "Sum"
  period      = 900

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1

  alarm_actions      = local.alarm_actions
  treat_missing_data = "notBreaching"

  tags = var.tags
}

# --------------------------------------------------------------------------
# Cost
#
# Budget notifications at 50/80/100% of actual and a forecast trigger. The
# forecast one is the useful member of the set: it fires while there is still
# a month left to react, rather than reporting a bill already incurred.
# --------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$${var.project_tag}"]
  }

  dynamic "notification" {
    for_each = var.budget_actual_thresholds
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
      subscriber_email_addresses = var.alert_email_addresses
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
    subscriber_email_addresses = var.alert_email_addresses
  }
}
