output "secret_arns" {
  description = "Secret ARNs keyed by short name. Pass only the ones a workload needs into the ecs module's secret_arns."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.arn }
}

output "secret_names" {
  description = "Full secret names keyed by short name."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.name }
}

output "all_secret_arns" {
  description = "Every secret ARN as a list. Convenient, but prefer naming the specific ARNs a role needs."
  value       = [for s in aws_secretsmanager_secret.this : s.arn]
}
