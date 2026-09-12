variable "name_prefix" {
  description = "Prefix applied to every resource name, e.g. hawknetic-prod."
  type        = string
}

variable "region" {
  description = "AWS region. Used to build VPC endpoint service names."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR, /16 or larger. Subnets are carved as twelve /4-offset blocks, so a /16 yields the documented /20 tiers."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    # Check the prefix length directly. cidrsubnet(x, 4, 11) succeeds for any
    # valid CIDR -- netnum 11 is always below 2^4 -- so the obvious can()
    # version of this validation accepted a /24 and silently produced /28
    # tiers instead of the /20s the module documents.
    condition     = can(cidrhost(var.cidr_block, 0)) && tonumber(split("/", var.cidr_block)[1]) <= 16
    error_message = "cidr_block must be a valid CIDR of /16 or larger; a smaller block yields subnets far below the documented /20 tiers."
  }
}

variable "availability_zones" {
  description = "Candidate AZs, in preference order. az_count are taken from the front. Leave null to look them up from the configured region -- pass an explicit list only to pin specific zones."
  type        = list(string)
  default     = null
}

variable "az_count" {
  description = "Number of AZs to span. Two is the minimum an ALB will accept."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3: an ALB requires two subnets, and a third AZ buys little here."
  }
}

variable "enable_nat_gateway" {
  description = <<-EOT
    true  -- ECS tasks run in private subnets and egress via NAT Gateway
             (~$32/month per gateway plus $0.045/GB). No public task addresses.
    false -- ECS tasks run in public subnets with a public IP and egress via the
             internet gateway ($0/month). The task security group permits no
             inbound rules, so nothing can reach them.

    RDS is in the isolated tier and unreachable from the internet either way.
  EOT
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Collapse to one NAT Gateway shared by all AZs. Halves (or thirds) the cost, at the price of an AZ-level single point of failure for egress."
  type        = bool
  default     = false
}

variable "enable_interface_endpoints" {
  description = "Create interface endpoints for ECR, CloudWatch Logs and Secrets Manager (~$7.30/month each per AZ). Only worth it when NAT is enabled and data-processing charges exceed the endpoint fee. The free S3 gateway endpoint is always created."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
