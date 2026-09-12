# ECS Fargate cluster, ALB, and the web/API service.
#
# Scope note: this module owns the cluster, the load balancer, the two IAM
# roles every task needs, and the one always-on HTTP service. Workers are not
# here -- always-on workers are separate ECS services and hourly workers are
# EventBridge Scheduler -> RunTask, both in modules/scheduler. Keeping web
# traffic and background work in different task definitions is deliberate: a
# collector that wedges must not be able to take the dashboard down with it.

data "aws_region" "current" {}

locals {
  # Two roles, and the split matters. The execution role is used by the ECS
  # agent before the container starts -- pulling the image, fetching secrets,
  # writing the log stream. The task role is what the application code itself
  # gets. Merging them would hand the application the ability to read every
  # secret the platform can inject, which is precisely what we are trying to
  # avoid.
  execution_role_arn = aws_iam_role.execution.arn
}

resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-cluster" })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name = aws_ecs_cluster.this.name

  # FARGATE_SPOT is up to ~70% cheaper and can be reclaimed with two minutes'
  # notice. Right for interruptible collectors, wrong for the dashboard, so the
  # choice is made per service rather than here.
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# --------------------------------------------------------------------------
# IAM
# --------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name = "${var.name_prefix}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Secrets the *agent* injects as container environment. Scoped to the exact
# ARNs passed in, never a wildcard: the managed policy above deliberately does
# not grant secretsmanager access, so this is the only path.
resource "aws_iam_role_policy" "execution_secrets" {
  count = length(var.secret_arns) > 0 ? 1 : 0

  name = "${var.name_prefix}-ecs-execution-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.secret_arns
      }],
      length(var.secret_kms_key_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.secret_kms_key_arns
      }] : [],
    )
  })
}

resource "aws_iam_role" "task" {
  name = "${var.name_prefix}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

# The application's own permissions. Empty by default -- it gets S3 access for
# report and raw-payload archival and nothing else unless something is added
# here on purpose.
resource "aws_iam_role_policy" "task_s3" {
  count = length(var.task_s3_bucket_arns) > 0 ? 1 : 0

  name = "${var.name_prefix}-ecs-task-s3"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.task_s3_bucket_arns
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [for arn in var.task_s3_bucket_arns : "${arn}/*"]
      },
    ]
  })
}

# --------------------------------------------------------------------------
# Security groups
# --------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public ingress to the ${var.name_prefix} load balancer"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# Port 80 exists only to redirect to 443; it never serves content.
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from the internet, redirected to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the application tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "tasks" {
  name        = "${var.name_prefix}-tasks"
  description = "ECS tasks for ${var.name_prefix}"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-tasks" })
}

# The only inbound rule tasks get. Note there is no rule permitting the
# internet to reach them: when the network module runs tasks in public subnets
# to avoid NAT charges, this security group is what makes that safe.
resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Application traffic from the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

# Outbound is open because the collectors fetch from Kalshi and the external
# sports and odds sources, which publish no stable address range to narrow to.
resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  description       = "Outbound to AWS APIs and external research sources"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# --------------------------------------------------------------------------
# Load balancer
# --------------------------------------------------------------------------

resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "prod"

  tags = merge(var.tags, { Name = "${var.name_prefix}-alb" })
}

resource "aws_lb_target_group" "web" {
  name        = "${var.name_prefix}-web"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  # /healthz is process liveness. /readyz additionally checks the database,
  # migration state and the safety flags -- correct for a readiness gate, wrong
  # for the ALB: a brief database blip would make the ALB kill every task and
  # turn a recoverable dependency failure into a full outage.
  health_check {
    enabled             = true
    path                = "/healthz"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Give in-flight requests time to finish on deploy, but not so long that a
  # rollout crawls.
  deregistration_delay = 30

  lifecycle {
    create_before_destroy = true
  }

  tags = var.tags
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = var.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }

  tags = var.tags
}

# --------------------------------------------------------------------------
# Web service
# --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${var.name_prefix}/web"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.name_prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = local.execution_role_arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    # The image is built for amd64. Switching this to ARM64 requires an
    # arm64 image, so the two must change together.
    cpu_architecture = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "web"
    image     = var.image
    essential = true

    portMappings = [{
      containerPort = var.container_port
      protocol      = "tcp"
    }]

    environment = [
      for k, v in merge(var.common_environment, { HAWKNETIC_SERVICE = "web" }) :
      { name = k, value = tostring(v) }
    ]

    secrets = [
      for k, v in var.secret_environment : { name = k, valueFrom = v }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.web.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "web"
      }
    }

    # stopTimeout gives the process time to finish in-flight work after
    # SIGTERM. The Dockerfile's exec-form entrypoint is what makes this
    # effective -- the signal reaches Python rather than a shell.
    stopTimeout = 30
  }])

  tags = var.tags
}

resource "aws_ecs_service" "web" {
  name            = "${var.name_prefix}-web"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = var.web_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.task_subnet_ids
    security_groups = [aws_security_group.tasks.id]
    # Required when tasks run in public subnets without NAT: with no public IP
    # and no NAT route the task cannot reach ECR and will never start.
    assign_public_ip = var.assign_task_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = var.container_port
  }

  # Circuit breaker with rollback: a bad image is reverted automatically
  # instead of sitting in a restart loop until someone notices.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  health_check_grace_period_seconds = 60

  # CI deploys new task definition revisions. Without this, every Terraform run
  # after a deploy would plan to revert the service to the revision Terraform
  # last knew about.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.https]

  tags = var.tags
}
