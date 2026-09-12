# VPC for the Hawknetic platform.
#
# Three subnet tiers:
#   public   -- ALB only (and, when enable_nat_gateway = false, the Fargate
#               tasks themselves; see the egress note below)
#   private  -- ECS tasks when NAT is enabled
#   isolated -- RDS. No route to the internet in either direction, ever.
#
# ## The egress decision, which is the main cost lever in this whole stack
#
# The workers fetch from Kalshi and the external sports/odds sources, so they
# need outbound internet. There are two ways to give a Fargate task that, and
# they differ by ~$33/month per AZ:
#
#   enable_nat_gateway = true   tasks sit in private subnets and egress through
#                               a NAT Gateway. ~$32/month each plus $0.045/GB
#                               processed. Tasks have no public address.
#
#   enable_nat_gateway = false  tasks sit in public subnets with a public IP and
#                               egress straight through the internet gateway.
#                               $0/month. Tasks have a public address, but the
#                               security group permits no inbound rules at all,
#                               so nothing can reach them.
#
# NAT is the more conventional posture and worth paying for in production. It
# is poor value in dev, where the workload is intermittent and the $32 is most
# of the environment's bill. Hence the variable, defaulted per environment
# rather than globally.
#
# Either way RDS stays in the isolated tier and is never reachable from the
# internet -- that is not what this switch controls.

# Look the zones up rather than hardcoding them. The default list used to be
# us-east-2a/b/c, which silently produced an invalid configuration the moment
# `region` was set to anything but Ohio -- the module would try to place
# subnets in availability zones that do not exist in the selected region.
# `state = "available"` also skips a zone that is impaired or not open to this
# account, which a static list cannot.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  available_azs = coalesce(var.availability_zones, data.aws_availability_zones.available.names)
  azs           = slice(local.available_azs, 0, var.az_count)

  # ECS tasks live in private subnets only when NAT exists to serve them.
  task_subnet_ids = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

# --------------------------------------------------------------------------
# Subnets
# --------------------------------------------------------------------------

resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 4, count.index)

  # Only meaningful when tasks run here (enable_nat_gateway = false); the ALB
  # gets its address from its own configuration either way.
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${local.azs[count.index]}"
    Tier = "public"
  })
}

resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 4, count.index + 4)

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-${local.azs[count.index]}"
    Tier = "private"
  })
}

resource "aws_subnet" "isolated" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 4, count.index + 8)

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-isolated-${local.azs[count.index]}"
    Tier = "isolated"
  })
}

# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-rt-public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One NAT Gateway per AZ is the resilient shape and twice the price. single_nat
# _gateway collapses to one, which is the right trade for this workload: a
# research collector that loses an AZ for an hour re-collects on its next
# cadence. Production defaults to resilient; the switch exists so the cost is a
# decision rather than an accident.
resource "aws_eip" "nat" {
  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : var.az_count) : 0

  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name_prefix}-nat-eip-${count.index}" })
}

resource "aws_nat_gateway" "this" {
  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : var.az_count) : 0

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index}" })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  count = var.az_count

  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-rt-private-${count.index}" })
}

resource "aws_route" "private_nat" {
  count = var.enable_nat_gateway ? var.az_count : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
}

resource "aws_route_table_association" "private" {
  count = var.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Isolated subnets get a route table with no internet route at all. Declaring
# it explicitly beats letting them fall back to the VPC main route table, whose
# contents are easy to change by accident somewhere else.
resource "aws_route_table" "isolated" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-rt-isolated" })
}

resource "aws_route_table_association" "isolated" {
  count = var.az_count

  subnet_id      = aws_subnet.isolated[count.index].id
  route_table_id = aws_route_table.isolated.id
}

# --------------------------------------------------------------------------
# VPC endpoints
#
# The S3 gateway endpoint is free and keeps ECR layer pulls -- which are S3
# reads, and the bulk of task-start traffic -- off the NAT Gateway entirely.
# That is the single highest-value endpoint here and it costs nothing, so it is
# unconditional.
#
# Interface endpoints are $0.01/hour each (~$7.30/month per endpoint per AZ)
# and only pay for themselves when they displace enough NAT data processing.
# With NAT disabled they displace nothing, so they are off by default and
# gated behind a variable rather than switched on reflexively.
# --------------------------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat(
    aws_route_table.private[*].id,
    [aws_route_table.isolated.id, aws_route_table.public.id],
  )

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce-s3" })
}

resource "aws_security_group" "endpoints" {
  count = var.enable_interface_endpoints ? 1 : 0

  name        = "${var.name_prefix}-vpce"
  description = "HTTPS from inside the VPC to interface VPC endpoints"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce" })
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  count = var.enable_interface_endpoints ? 1 : 0

  security_group_id = aws_security_group.endpoints[0].id
  description       = "HTTPS from within the VPC"
  cidr_ipv4         = var.cidr_block
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_endpoint" "interface" {
  for_each = var.enable_interface_endpoints ? toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
  ]) : toset([])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce-${each.value}" })
}
