# Background workers: always-on ECS services and scheduled EventBridge tasks.
#
# The application has eight worker roles, each selected by HAWKNETIC_SERVICE.
# They split cleanly by cadence, and the split is where the money is:
#
#   always-on (mode = "service")   kalshi-market-ingestion   300 s
#                                  external-source-ingestion 900 s
#                                  crypto-research           900 s
#
#   scheduled (mode = "scheduled") sports-research           hourly
#                                  research-model-refresh    hourly
#                                  settlement-worker         hourly
#                                  raw-retention             hourly
#                                  reporting-evaluation      6-hourly
#
# On Railway all eight are resident processes, so the six-hourly reporting
# worker is billed for 21,600 seconds to do a few seconds of work. Fargate
# bills per second of task life, so running the slow five as scheduled tasks
# costs their execution time and nothing between runs.
#
# The mechanism is HAWKNETIC_SERVICE_MODE=once, which runs a single cycle and
# exits. The cutover is safe because run_worker_once claims a cadence-derived
# idempotency key: if a scheduled run overlaps a still-running loop worker, the
# second records skipped_duplicate rather than collecting twice.
#
# EventBridge Scheduler is used rather than EventBridge Rules: it does
# one-schedule-one-target natively, supports a flexible time window to spread
# load, and does not need a rule plus target plus permission triple per job.

data "aws_region" "current" {}

locals {
  services  = { for k, v in var.workers : k => v if v.mode == "service" }
  scheduled = { for k, v in var.workers : k => v if v.mode == "scheduled" }
}

resource "aws_cloudwatch_log_group" "worker" {
  for_each = var.workers

  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_ecs_task_definition" "worker" {
  for_each = var.workers

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = each.key
    image     = var.image
    essential = true

    # Order matters: per-worker overrides are merged BEFORE the two reserved
    # variables, so a worker cannot override them. They are not settings, they
    # are what makes this task the worker it claims to be -- a worker map that
    # set HAWKNETIC_SERVICE would run a different role than its schedule and
    # log group say, and one that set HAWKNETIC_SERVICE_MODE=loop would turn a
    # scheduled task into a resident process that never exits.
    environment = [
      for k, v in merge(
        var.common_environment,
        each.value.environment,
        {
          HAWKNETIC_SERVICE = each.key
          # A scheduled task must exit after one cycle, or it would run until
          # the next schedule fires and defeat the whole arrangement.
          HAWKNETIC_SERVICE_MODE = each.value.mode == "scheduled" ? "once" : "loop"
        },
      ) : { name = k, value = tostring(v) }
    ]

    # Shared secrets (the database credential) plus whatever this worker
    # specifically needs. Per-worker entries win on a name collision.
    secrets = [
      for k, v in merge(var.secret_environment, each.value.secret_environment) :
      { name = k, valueFrom = v }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker[each.key].name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = each.key
      }
    }

    # Workers hold database transactions. SIGTERM must reach the process with
    # enough time to commit or roll back cleanly.
    stopTimeout = 60
  }])

  tags = merge(var.tags, { Worker = each.key })
}

# --------------------------------------------------------------------------
# Always-on workers
# --------------------------------------------------------------------------

resource "aws_ecs_service" "worker" {
  for_each = local.services

  name            = "${var.name_prefix}-${each.key}"
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.worker[each.key].arn
  desired_count   = 1

  # Collectors are interruptible by design -- a reclaimed task re-collects on
  # its next cadence -- so Spot is a sound trade here in a way it is not for
  # the dashboard.
  capacity_provider_strategy {
    capacity_provider = each.value.use_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [var.task_security_group_id]
    assign_public_ip = var.assign_task_public_ip
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A worker claims a cadence-bucket idempotency key, so two of them
  # overlapping *inside the same bucket* deduplicate: the second records
  # skipped_duplicate. That protection is bucket-scoped, not general -- a loop
  # cycle that runs long enough to cross a bucket boundary can be joined by a
  # scheduled run in the next bucket and both will collect. Replacing rather
  # than doubling keeps exactly one collector live, which is what actually
  # keeps the overlap window closed here.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  tags = merge(var.tags, { Worker = each.key })
}

# --------------------------------------------------------------------------
# Scheduled workers
# --------------------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  count = length(local.scheduled) > 0 ? 1 : 0

  name = "${var.name_prefix}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        # Without this the role is assumable by the scheduler service on behalf
        # of any account. It costs nothing to scope and is the difference
        # between a confused-deputy and a closed one.
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "scheduler" {
  count = length(local.scheduled) > 0 ? 1 : 0

  name = "${var.name_prefix}-scheduler"
  role = aws_iam_role.scheduler[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect = "Allow"
          Action = ["ecs:RunTask"]
          # Scoped to this family's revisions only -- ":*" is the revision
          # wildcard, not a resource wildcard.
          Resource = [for k, _ in local.scheduled : "${aws_ecs_task_definition.worker[k].arn_without_revision}:*"]
          Condition = {
            ArnEquals = { "ecs:cluster" = var.cluster_arn }
          }
        },
        {
          # RunTask with a task role requires the scheduler to pass both roles.
          Effect   = "Allow"
          Action   = ["iam:PassRole"]
          Resource = [var.execution_role_arn, var.task_role_arn]
          Condition = {
            StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
          }
        },
      ],
      # EventBridge Scheduler writes to the dead-letter queue as this role.
      # Without sqs:SendMessage every DLQ delivery fails with AccessDenied and
      # the queue stays empty -- hiding exactly the failures it was added to
      # surface, and doing so silently.
      var.dead_letter_queue_arn == null ? [] : [{
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [var.dead_letter_queue_arn]
      }],
    )
  })
}

resource "aws_scheduler_schedule" "worker" {
  for_each = local.scheduled

  name       = "${var.name_prefix}-${each.key}"
  group_name = "default"

  # Every hourly job firing exactly on the hour would stampede the database and
  # the external sources at once. A flexible window lets EventBridge spread
  # them, and these cycles have no reason to be punctual to the second.
  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = each.value.flex_window_minutes
  }

  schedule_expression          = each.value.schedule
  schedule_expression_timezone = var.schedule_timezone
  state                        = each.value.enabled ? "ENABLED" : "DISABLED"

  target {
    arn      = var.cluster_arn
    role_arn = aws_iam_role.scheduler[0].arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.worker[each.key].arn_without_revision
      launch_type         = "FARGATE"
      task_count          = 1
      propagate_tags      = "TASK_DEFINITION"

      network_configuration {
        subnets          = var.task_subnet_ids
        security_groups  = [var.task_security_group_id]
        assign_public_ip = var.assign_task_public_ip
      }
    }

    # This retries *invocation delivery* -- EventBridge failing to start the
    # task at all. It does not retry a task that started and exited nonzero;
    # a failed worker cycle is picked up by the next schedule instead. The
    # short tail keeps a retry from overlapping that next run.
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 600
    }

    dynamic "dead_letter_config" {
      for_each = var.dead_letter_queue_arn == null ? [] : [var.dead_letter_queue_arn]
      content {
        arn = dead_letter_config.value
      }
    }
  }
}
