variable "name_prefix" {
  description = "Prefix applied to every resource name, e.g. hawknetic-prod."
  type        = string
}

variable "account_id" {
  description = "AWS account id, used to scope the scheduler role's trust policy to this account."
  type        = string
}

variable "cluster_arn" {
  description = "ECS cluster ARN from the ecs module."
  type        = string
}

variable "image" {
  description = "Full image reference. Same image as the web service -- the role is chosen by HAWKNETIC_SERVICE."
  type        = string
}

variable "execution_role_arn" {
  description = "Task execution role ARN from the ecs module."
  type        = string
}

variable "task_role_arn" {
  description = "Application task role ARN from the ecs module."
  type        = string
}

variable "task_subnet_ids" {
  description = "Subnets for worker tasks. Use the network module's task_subnet_ids."
  type        = list(string)
}

variable "task_security_group_id" {
  description = "Task security group from the ecs module. Reused so workers reach the database through the same allowed group."
  type        = string
}

variable "assign_task_public_ip" {
  description = "Give worker tasks a public IP. Must be true when running without NAT."
  type        = bool
  default     = false
}

variable "workers" {
  description = <<-EOT
    Worker roles to deploy, keyed by HAWKNETIC_SERVICE value.

    mode = "service"    always-on ECS service, HAWKNETIC_SERVICE_MODE=loop
    mode = "scheduled"  EventBridge Scheduler -> RunTask, MODE=once

    `schedule` is required for scheduled workers and is an EventBridge
    Scheduler expression, e.g. "cron(0 * * * ? *)" or "rate(1 hour)".
  EOT
  type = map(object({
    mode                = string
    cpu                 = optional(number, 256)
    memory              = optional(number, 512)
    use_spot            = optional(bool, false)
    schedule            = optional(string)
    flex_window_minutes = optional(number, 5)
    enabled             = optional(bool, true)
    environment         = optional(map(string), {})
    # Secrets only this worker needs, as name => secret ARN, merged over the
    # module-level secret_environment. Least privilege is the point: the
    # ingestion collector should get the Kalshi credential and the reporting
    # worker should not. Every ARN used here must also be readable by the
    # execution role passed in, or the task fails to start.
    secret_environment = optional(map(string), {})
  }))

  validation {
    condition     = alltrue([for w in var.workers : contains(["service", "scheduled"], w.mode)])
    error_message = "Each worker's mode must be \"service\" or \"scheduled\"."
  }

  validation {
    condition     = alltrue([for w in var.workers : w.mode != "scheduled" || w.schedule != null])
    error_message = "A worker with mode=\"scheduled\" must set schedule; without one it would never run."
  }

  validation {
    condition     = alltrue([for w in var.workers : w.mode != "service" || w.schedule == null])
    error_message = "A worker with mode=\"service\" must not set schedule; an always-on service runs on its own cadence and the schedule would be silently ignored."
  }
}

variable "common_environment" {
  description = "Non-secret environment variables shared by every worker."
  type        = map(string)
  default     = {}
}

variable "secret_environment" {
  description = "Environment variables sourced from Secrets Manager, as name => secret ARN."
  type        = map(string)
  default     = {}
}

variable "schedule_timezone" {
  description = "Timezone for schedule expressions. UTC keeps cadences stable across daylight-saving transitions, which is what these cycles want."
  type        = string
  default     = "UTC"
}

variable "dead_letter_queue_arn" {
  description = "Optional SQS queue for schedule invocations that could not be delivered. This catches failures to *start* a task, not failures inside one."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "CloudWatch log retention for worker log groups."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
