output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in use, whether created here or supplied."
  value       = local.oidc_provider_arn
}

output "role_arns" {
  description = "Role ARNs keyed by the short name used in var.roles. These are what a workflow passes to aws-actions/configure-aws-credentials as role-to-assume."
  value       = { for k, r in aws_iam_role.this : k => r.arn }
}

output "role_names" {
  description = "Role names keyed by short name."
  value       = { for k, r in aws_iam_role.this : k => r.name }
}
