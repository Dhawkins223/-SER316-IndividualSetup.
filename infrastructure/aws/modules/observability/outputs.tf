output "alert_topic_arn" {
  description = "SNS topic every alarm publishes to. Email subscribers must confirm before delivery begins."
  value       = aws_sns_topic.alerts.arn
}

output "alarm_names" {
  description = "Names of the alarms created by this module."
  value = compact([
    aws_cloudwatch_metric_alarm.rds_free_storage.alarm_name,
    aws_cloudwatch_metric_alarm.rds_cpu.alarm_name,
    aws_cloudwatch_metric_alarm.rds_connections.alarm_name,
    aws_cloudwatch_metric_alarm.alb_unhealthy_hosts.alarm_name,
    aws_cloudwatch_metric_alarm.alb_5xx.alarm_name,
    try(aws_cloudwatch_metric_alarm.scheduler_failures[0].alarm_name, ""),
  ])
}

output "budget_name" {
  description = "Monthly budget name."
  value       = aws_budgets_budget.monthly.name
}
