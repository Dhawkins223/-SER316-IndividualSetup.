output "state_bucket_name" {
  description = "State bucket name. Use this as `bucket` in every environment's backend block."
  value       = aws_s3_bucket.state.id
}

output "state_bucket_arn" {
  description = "State bucket ARN, for scoping a CI role's access to it."
  value       = aws_s3_bucket.state.arn
}

output "backend_config_hint" {
  description = "Backend block to paste into an environment, with its own key."
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket       = "${aws_s3_bucket.state.id}"
        key          = "<environment>/terraform.tfstate"
        region       = "${var.region}"
        encrypt      = true
        use_lockfile = true
      }
    }
  EOT
}
