variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-2"
}

variable "github_repository" {
  description = "Repository allowed to assume the deployment roles, as owner/repo."
  type        = string
  default     = "Dhawkins223/HawkNeticSportsTools"
}

variable "state_bucket_arn" {
  description = "ARN of the Terraform state bucket from the bootstrap stack. The plan role is granted state access on this and nothing else."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR."
  type        = string
  default     = "10.40.0.0/16"
}

variable "single_nat_gateway" {
  description = "Share one NAT Gateway across AZs. Saves ~$32/month and makes egress single-AZ. Production defaults to the resilient shape."
  type        = bool
  default     = false
}

variable "enable_interface_endpoints" {
  description = "Create ECR/Logs/Secrets interface endpoints (~$7.30/month each per AZ). Only worth it once NAT data-processing charges exceed that; measure before enabling."
  type        = bool
  default     = false
}

variable "image" {
  description = "Container image reference for the initial task definitions. CI replaces this on each deploy, and the ECS module ignores subsequent task_definition drift."
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate ARN for the HTTPS listener, in the same region as the ALB."
  type        = string
}

variable "web_cpu" {
  description = "Fargate CPU units for the web task."
  type        = number
  default     = 512
}

variable "web_memory" {
  description = "Fargate memory (MiB) for the web task."
  type        = number
  default     = 1024
}

variable "web_desired_count" {
  description = "Web task count. Two gives a zero-downtime deploy and survives an AZ loss."
  type        = number
  default     = 2
}

variable "rds_engine_version" {
  description = "PostgreSQL version. Verify availability in-region before applying: aws rds describe-db-engine-versions --engine postgres --region us-east-2 --query 'DBEngineVersions[].EngineVersion'"
  type        = string
  default     = "18.1"
}

variable "rds_engine_major_version" {
  description = "Major version for the parameter group family."
  type        = string
  default     = "18"
}

variable "rds_instance_class" {
  description = "RDS instance class. db.t4g.small is Graviton and a reasonable production floor for this workload; measure before going larger."
  type        = string
  default     = "db.t4g.small"
}

variable "rds_allocated_storage_gb" {
  description = "Initial RDS storage. The source database is ~5 GB; this starts above it with autoscaling behind."
  type        = number
  default     = 50
}

variable "rds_max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling. This is the structural fix for the Railway full-volume incident."
  type        = number
  default     = 200
}

variable "rds_multi_az" {
  description = "Multi-AZ. Roughly doubles the instance cost for automatic failover. Off until the workload justifies it."
  type        = bool
  default     = false
}

variable "rds_free_storage_threshold_gb" {
  description = "Alarm below this much free RDS storage."
  type        = number
  default     = 20
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

variable "alert_email_addresses" {
  description = "Addresses for alarm and budget notifications. Each must confirm its SNS subscription by email."
  type        = list(string)
  default     = []
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD. See docs/aws-migration/cost-model.md for the derivation rather than picking a round number."
  type        = number
  default     = 180
}
