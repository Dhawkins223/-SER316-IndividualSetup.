output "cluster_id" {
  description = "ECS cluster id."
  value       = aws_ecs_cluster.this.id
}

output "cluster_arn" {
  description = "ECS cluster ARN, needed by EventBridge Scheduler RunTask targets."
  value       = aws_ecs_cluster.this.arn
}

output "cluster_name" {
  description = "ECS cluster name, for CloudWatch alarm dimensions."
  value       = aws_ecs_cluster.this.name
}

output "task_security_group_id" {
  description = "Security group shared by all tasks. Pass this to the RDS module's allowed_security_group_ids so tasks can reach the database."
  value       = aws_security_group.tasks.id
}

output "alb_security_group_id" {
  description = "Load balancer security group."
  value       = aws_security_group.alb.id
}

output "alb_dns_name" {
  description = "ALB hostname. Route 53 alias target for the public record."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone id, for a Route 53 alias record."
  value       = aws_lb.this.zone_id
}

output "alb_arn_suffix" {
  description = "ALB ARN suffix, the dimension CloudWatch uses for ALB metrics."
  value       = aws_lb.this.arn_suffix
}

output "target_group_arn_suffix" {
  description = "Target group ARN suffix, the dimension for UnHealthyHostCount."
  value       = aws_lb_target_group.web.arn_suffix
}

output "execution_role_arn" {
  description = "Task execution role ARN. Reused by worker and scheduled task definitions."
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "Application task role ARN."
  value       = aws_iam_role.task.arn
}

output "web_service_name" {
  description = "Web ECS service name, for deployments and alarms."
  value       = aws_ecs_service.web.name
}

output "web_log_group_name" {
  description = "CloudWatch log group for the web service."
  value       = aws_cloudwatch_log_group.web.name
}
