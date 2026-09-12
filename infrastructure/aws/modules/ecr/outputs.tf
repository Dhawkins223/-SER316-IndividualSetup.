output "repository_url" {
  description = "Repository URL, for docker push and for a task definition image reference."
  value       = aws_ecr_repository.this.repository_url
}

output "repository_arn" {
  description = "Repository ARN, for scoping a CI role's ecr: permissions to this one repository."
  value       = aws_ecr_repository.this.arn
}

output "repository_name" {
  description = "Repository name."
  value       = aws_ecr_repository.this.name
}
