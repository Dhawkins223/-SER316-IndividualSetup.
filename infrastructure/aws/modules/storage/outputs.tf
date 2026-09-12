output "bucket_names" {
  description = "Bucket names keyed by suffix."
  value       = { for k, b in aws_s3_bucket.this : k => b.id }
}

output "bucket_arns" {
  description = "Bucket ARNs keyed by suffix. Pass the ones a workload needs to the ecs module's task_s3_bucket_arns."
  value       = { for k, b in aws_s3_bucket.this : k => b.arn }
}

output "all_bucket_arns" {
  description = "Every bucket ARN as a list."
  value       = [for b in aws_s3_bucket.this : b.arn]
}
