output "alb_dns_name" {
  description = "Load balancer hostname. This is the Route 53 alias target when cutover is approved -- creating that record is a separate, explicit act."
  value       = module.ecs.alb_dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone id for a Route 53 alias record."
  value       = module.ecs.alb_zone_id
}

output "ecr_repository_url" {
  description = "ECR repository URL for CI to push to."
  value       = module.ecr.repository_url
}

output "rds_endpoint" {
  description = "RDS endpoint, host:port. Reachable only from the task security group."
  value       = module.rds.endpoint
}

output "rds_master_user_secret_arn" {
  description = "Secrets Manager ARN of the RDS-managed master credential."
  value       = module.rds.master_user_secret_arn
}

output "github_actions_role_arns" {
  description = "Role ARNs for aws-actions/configure-aws-credentials. No static access keys are involved."
  value       = module.github_oidc.role_arns
}

output "alert_topic_arn" {
  description = "SNS topic every alarm publishes to."
  value       = module.observability.alert_topic_arn
}

output "s3_bucket_names" {
  description = "Created S3 buckets, keyed by purpose."
  value       = module.storage.bucket_names
}

output "worker_schedule_names" {
  description = "EventBridge schedule names for the scheduled workers."
  value       = module.workers.schedule_names
}
