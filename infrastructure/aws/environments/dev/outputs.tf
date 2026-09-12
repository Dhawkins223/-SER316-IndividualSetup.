output "alb_dns_name" {
  description = "Load balancer hostname."
  value       = module.ecs.alb_dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL for CI to push to."
  value       = module.ecr.repository_url
}

output "rds_endpoint" {
  description = "RDS endpoint, host:port."
  value       = module.rds.endpoint
}

output "github_actions_role_arns" {
  description = "Role ARNs for aws-actions/configure-aws-credentials."
  value       = module.github_oidc.role_arns
}

output "tasks_have_public_ip" {
  description = "True when tasks egress through the internet gateway rather than NAT. Expected true in dev."
  value       = module.network.tasks_need_public_ip
}
