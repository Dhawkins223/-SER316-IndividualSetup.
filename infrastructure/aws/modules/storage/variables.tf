variable "name_prefix" {
  description = "Prefix applied to bucket names. Combined with the map key to form the bucket name, which must be globally unique."
  type        = string
}

variable "environment" {
  description = "Environment name. 'prod' blocks force_destroy."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "buckets" {
  description = <<-EOT
    Buckets to create, keyed by suffix. Each becomes "<name_prefix>-<key>".

    transition_to_ia_days moves objects to Standard-IA, which is cheaper to
    store and dearer to read -- right for archived payloads, wrong for anything
    read routinely. Standard-IA also has a 30-day minimum billable lifetime, so
    a value below 30 costs more than it saves.
  EOT
  type = map(object({
    purpose                 = string
    versioned               = optional(bool, false)
    transition_to_ia_days   = optional(number)
    expire_days             = optional(number)
    noncurrent_version_days = optional(number, 30)
  }))

  validation {
    condition = alltrue([
      for b in var.buckets :
      b.transition_to_ia_days == null || b.transition_to_ia_days >= 30
    ])
    error_message = "transition_to_ia_days must be at least 30: Standard-IA bills a 30-day minimum per object, so transitioning sooner increases cost."
  }

  validation {
    condition = alltrue([
      for b in var.buckets :
      b.expire_days == null || b.transition_to_ia_days == null || b.expire_days > b.transition_to_ia_days
    ])
    error_message = "expire_days must be greater than transition_to_ia_days, otherwise objects are deleted before the transition can apply."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
