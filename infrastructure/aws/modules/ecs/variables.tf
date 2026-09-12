variable "name_prefix" {
  description = "Prefix applied to every resource name, e.g. hawknetic-prod."
  type        = string
}

variable "environment" {
  description = "Environment name. 'prod' enables ALB deletion protection."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnets for the ALB. At least two, in different AZs."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "An ALB requires subnets in at least two availability zones."
  }
}

variable "task_subnet_ids" {
  description = "Subnets for ECS tasks. Take this from the network module's task_subnet_ids output, which already accounts for the NAT/public-IP egress mode."
  type        = list(string)
}

variable "assign_task_public_ip" {
  description = "Give tasks a public IP. Must be true when the network module runs without NAT, or tasks cannot reach ECR and will fail to start. Use the network module's tasks_need_public_ip output."
  type        = bool
  default     = false
}

variable "image" {
  description = "Full image reference for the task definition. Prefer a digest or immutable tag over a moving tag."
  type        = string
}

variable "container_port" {
  description = "Port the application listens on. The image defaults PORT to 8000."
  type        = number
  default     = 8000
}

variable "certificate_arn" {
  description = "ACM certificate ARN for the HTTPS listener. Must be in the same region as the ALB."
  type        = string
}

variable "web_cpu" {
  description = "Fargate CPU units for the web task. 256 = 0.25 vCPU."
  type        = number
  default     = 256
}

variable "web_memory" {
  description = "Fargate memory (MiB) for the web task. Must be a valid pairing with web_cpu -- enforced by a check block in main.tf, since a variable validation cannot reference another variable."
  type        = number
  default     = 512
}

variable "web_desired_count" {
  description = "Number of web tasks. Two gives a zero-downtime deploy and survives an AZ loss; one is fine for dev."
  type        = number
  default     = 2
}

variable "common_environment" {
  description = "Non-secret environment variables for every container. The research-only safety flags belong here as well as in the image, so the running configuration is auditable from the task definition."
  type        = map(string)
  default     = {}
}

variable "secret_environment" {
  description = "Environment variables sourced from Secrets Manager or SSM, as name => secret ARN. Use arn:...:secret-name:json-key:: to select a single JSON key."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = "Secret ARNs the execution role may read. Must cover everything referenced in secret_environment, or tasks fail to start with a secrets-retrieval error."
  type        = list(string)
  default     = []
}

variable "secret_kms_key_arns" {
  description = "KMS key ARNs needed to decrypt those secrets. Required for customer-managed keys, such as the RDS-managed master secret."
  type        = list(string)
  default     = []
}

variable "task_s3_bucket_arns" {
  description = "S3 bucket ARNs the application itself may read and write, for report and raw-payload archival. Empty means the task role gets no S3 access at all."
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Indefinite retention is a slow, silent cost."
  type        = number
  default     = 30
}

variable "container_insights" {
  description = "ECS Container Insights. Useful metrics, but it is billed per metric and adds up across many small services."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
