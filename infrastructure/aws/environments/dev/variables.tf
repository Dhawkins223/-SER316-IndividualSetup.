variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-2"
}

variable "github_repository" {
  description = "Repository allowed to assume the deployment role, as owner/repo."
  type        = string
  default     = "Dhawkins223/HawkNeticSportsTools"
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider here. It is account-global: if dev and prod share an account, exactly one stack may create it."
  type        = bool
  default     = false
}

variable "existing_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN, used when create_oidc_provider is false."
  type        = string
  default     = null
}

variable "vpc_cidr" {
  description = "VPC CIDR. Distinct from production so the two can be peered later without renumbering."
  type        = string
  default     = "10.41.0.0/16"
}

variable "image" {
  description = "Container image reference for the initial task definitions."
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate ARN for the HTTPS listener."
  type        = string
}

variable "rds_engine_version" {
  description = "PostgreSQL version. Verify in-region availability before applying."
  type        = string
  default     = "18.1"
}

variable "rds_engine_major_version" {
  description = "Major version for the parameter group family."
  type        = string
  default     = "18"
}

variable "rds_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "rds_allocated_storage_gb" {
  description = "Initial RDS storage."
  type        = number
  default     = 20
}

variable "rds_max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling."
  type        = number
  default     = 50
}

variable "alert_email_addresses" {
  description = "Addresses for alarm and budget notifications."
  type        = list(string)
  default     = []
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD for the dev environment."
  type        = number
  default     = 60
}
