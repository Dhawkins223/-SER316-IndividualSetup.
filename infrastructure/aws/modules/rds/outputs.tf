output "instance_id" {
  description = "RDS instance identifier, for CloudWatch alarm dimensions."
  value       = aws_db_instance.this.id
}

output "instance_arn" {
  description = "RDS instance ARN."
  value       = aws_db_instance.this.arn
}

output "endpoint" {
  description = "Connection endpoint, host:port."
  value       = aws_db_instance.this.endpoint
}

output "address" {
  description = "Connection hostname."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Connection port."
  value       = aws_db_instance.this.port
}

output "database_name" {
  description = "Initial database name."
  value       = aws_db_instance.this.db_name
}

output "security_group_id" {
  description = "The database security group. Add workloads by passing their security group id into allowed_security_group_ids."
  value       = aws_security_group.this.id
}

output "master_user_secret_arn" {
  description = <<-EOT
    ARN of the RDS-managed Secrets Manager secret holding the master
    credentials. Grant a task role secretsmanager:GetSecretValue on this ARN
    rather than copying the password anywhere.

    The secret's value is a JSON document with `username` and `password`; it
    does not contain a ready-made connection URL. Whatever composes
    DATABASE_URL must build it from these plus the endpoint output above.
  EOT
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "kms_key_arn" {
  description = "KMS key protecting storage, Performance Insights and the master secret."
  value       = aws_kms_key.this.arn
}
