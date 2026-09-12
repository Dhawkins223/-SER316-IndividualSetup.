variable "name_prefix" {
  description = "Prefix applied to every resource name, e.g. hawknetic-prod."
  type        = string
}

variable "project_tag" {
  description = "Value of the Project tag, used to scope the budget's cost filter to this project's resources."
  type        = string
  default     = "hawknetic"
}

variable "alert_email_addresses" {
  description = "Addresses subscribed to the alert topic and budget notifications. Each must confirm the SNS subscription by email before it receives anything."
  type        = list(string)
  default     = []
}

variable "rds_instance_id" {
  description = "RDS instance identifier for alarm dimensions."
  type        = string
}

variable "rds_free_storage_threshold_gb" {
  description = "Alarm when RDS free storage falls below this many GB. Set it well above the point of no return: the aim is time to act, not notification of a fait accompli."
  type        = number
  default     = 10
}

variable "rds_cpu_threshold" {
  description = "Alarm when sustained RDS CPU exceeds this percentage."
  type        = number
  default     = 80
}

variable "rds_connection_threshold" {
  description = "Alarm above this many database connections. Size it from DATABASE_POOL_MAX_SIZE times the number of running tasks, with headroom."
  type        = number
  default     = 50
}

variable "alb_arn_suffix" {
  description = "ALB ARN suffix from the ecs module."
  type        = string
}

variable "target_group_arn_suffix" {
  description = "Target group ARN suffix from the ecs module."
  type        = string
}

variable "alb_5xx_threshold" {
  description = "Alarm above this many target 5xx responses in five minutes."
  type        = number
  default     = 10
}

variable "enable_scheduler_alarm" {
  description = "Create the EventBridge Scheduler failure alarm. Only useful when scheduled workers exist."
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD. Derive it from a measured baseline rather than guessing -- see docs/aws-migration/cost-model.md."
  type        = number

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be greater than zero."
  }
}

variable "budget_actual_thresholds" {
  description = "Percentages of the budget at which to notify on actual spend. A forecast notification at 100% is always added as well."
  type        = list(number)
  default     = [50, 80, 100, 120]
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
