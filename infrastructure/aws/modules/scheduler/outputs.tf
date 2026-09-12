output "service_names" {
  description = "Always-on worker ECS service names, keyed by worker role."
  value       = { for k, s in aws_ecs_service.worker : k => s.name }
}

output "schedule_names" {
  description = "EventBridge schedule names, keyed by worker role."
  value       = { for k, s in aws_scheduler_schedule.worker : k => s.name }
}

output "task_definition_arns" {
  description = "Task definition ARNs, keyed by worker role."
  value       = { for k, t in aws_ecs_task_definition.worker : k => t.arn }
}

output "log_group_names" {
  description = "CloudWatch log group names, keyed by worker role."
  value       = { for k, g in aws_cloudwatch_log_group.worker : k => g.name }
}
