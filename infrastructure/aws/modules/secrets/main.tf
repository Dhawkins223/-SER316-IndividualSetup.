# Secrets Manager containers, split by environment and workload.
#
# Deliberately many small secrets rather than one large one. A single
# "prod/all" secret means every task that needs any credential can read all of
# them, and least privilege becomes unexpressible -- the ingestion worker would
# hold the dashboard password because they share a document.
#
# This module creates the secret *containers* and leaves their values empty.
# Values are written out of band -- by the owner, or by RDS for the database
# credential it manages itself -- so no credential passes through Terraform
# configuration, state, or a plan output.
#
# Writing a value here would put it in state in plaintext. That is the whole
# reason `secret_string` is absent.

resource "aws_secretsmanager_secret" "this" {
  for_each = var.secrets

  name        = "${var.environment}/${each.key}"
  description = each.value.description

  # Short in dev so a mistaken name can be recreated the same day; the AWS
  # minimum is 7. Production keeps the default 30-day window, which is a real
  # safety net against a destroy nobody meant.
  recovery_window_in_days = var.environment == "prod" ? 30 : 7

  tags = merge(var.tags, { Name = "${var.environment}/${each.key}", Workload = each.value.workload })
}

# A placeholder version so a task referencing the secret fails with a clear
# "value not set" rather than a confusing retrieval error. Terraform stops
# tracking the content immediately afterwards, so the real value can be written
# out of band without showing up as drift on every plan.
resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each = var.secrets

  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = jsonencode({ placeholder = "set-out-of-band" })

  lifecycle {
    ignore_changes = [secret_string, version_stages]
  }
}
