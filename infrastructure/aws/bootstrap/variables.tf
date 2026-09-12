variable "region" {
  description = "AWS region for the state bucket. Keep it with the workloads unless there is a reason not to."
  type        = string
  default     = "us-east-2"
}

variable "state_bucket_name" {
  description = "Override the state bucket name. Defaults to hawknetic-tfstate-<account id>, which is unique without needing a decision."
  type        = string
  default     = null
}

variable "state_version_retention_days" {
  description = "How long to keep non-current state versions. Long enough to recover from a bad apply; state files are not an archive."
  type        = number
  default     = 90
}
