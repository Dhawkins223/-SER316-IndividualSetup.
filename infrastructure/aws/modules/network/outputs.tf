output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "VPC CIDR, for security group rules scoped to the VPC."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet ids. The ALB lives here."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet ids. Routed through NAT only when enable_nat_gateway is true."
  value       = aws_subnet.private[*].id
}

output "isolated_subnet_ids" {
  description = "Isolated subnet ids. RDS lives here; no internet route in either direction."
  value       = aws_subnet.isolated[*].id
}

output "task_subnet_ids" {
  description = "Subnets ECS tasks should run in, chosen by the egress mode. Pass straight to a service or scheduled task."
  value       = local.task_subnet_ids
}

output "tasks_need_public_ip" {
  description = "Whether ECS tasks must be given a public IP to reach the internet. True exactly when NAT is disabled, because then egress is via the internet gateway."
  value       = !var.enable_nat_gateway
}

output "nat_gateway_ids" {
  description = "NAT Gateway ids, empty when egress is via the internet gateway."
  value       = aws_nat_gateway.this[*].id
}
