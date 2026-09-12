# S3 buckets for artifacts, archived raw payloads, and migration dumps.
#
# The raw-payload bucket is the structural fix for the incident that prompted
# this migration. Raw research payload *bodies* grew at ~166 MB/day inside
# PostgreSQL and filled a 5 GB volume. Payload bodies are large, written once
# and read rarely -- an object store is where they belong.
#
# What does NOT move: the relational rows that reference them. Moving
# operational tables to S3 to save space would trade a storage problem for a
# correctness one.

resource "aws_s3_bucket" "this" {
  for_each = var.buckets

  bucket = "${var.name_prefix}-${each.key}"

  # Production buckets hold the migration dumps and the archived payloads. A
  # destroy must not be able to take them silently.
  force_destroy = var.environment != "prod"

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.key}", Purpose = each.value.purpose })
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # Cuts KMS/S3 request costs on buckets written in bulk; harmless elsewhere.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  versioning_configuration {
    status = each.value.versioned ? "Enabled" : "Disabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  # Incomplete multipart uploads are invisible in the console's object list and
  # billed indefinitely. A large dump that fails halfway leaves exactly this.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  dynamic "rule" {
    for_each = each.value.transition_to_ia_days == null ? [] : [each.value.transition_to_ia_days]
    content {
      id     = "transition-infrequent-access"
      status = "Enabled"

      filter {}

      transition {
        days          = rule.value
        storage_class = "STANDARD_IA"
      }
    }
  }

  dynamic "rule" {
    for_each = each.value.expire_days == null ? [] : [each.value.expire_days]
    content {
      id     = "expire"
      status = "Enabled"

      filter {}

      expiration {
        days = rule.value
      }
    }
  }

  # Old versions are billed at full rate forever unless told otherwise, which
  # makes versioning a quiet cost leak on a bucket that is written often.
  dynamic "rule" {
    for_each = each.value.versioned ? [1] : []
    content {
      id     = "expire-noncurrent-versions"
      status = "Enabled"

      filter {}

      noncurrent_version_expiration {
        noncurrent_days = each.value.noncurrent_version_days
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_policy" "require_tls" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.this[each.key].arn,
        "${aws_s3_bucket.this[each.key].arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}
