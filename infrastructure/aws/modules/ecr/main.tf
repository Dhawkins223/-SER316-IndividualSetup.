# ECR repository for the application image.
#
# One repository. The web role and all eight worker roles run the same image
# selected by HAWKNETIC_SERVICE, so there is one artifact to store, scan and
# promote -- see the Dockerfile for why.
#
# The lifecycle policy is the part that matters operationally. ECR bills
# $0.10/GB-month and a CI pipeline that pushes on every merge will accumulate
# hundreds of ~240 MB images. Untagged layers from overwritten tags are the
# worst of it: invisible in the console's default view and billed all the same.

resource "aws_ecr_repository" "this" {
  name                 = var.repository_name
  image_tag_mutability = var.image_tag_mutability

  # Scan on push is free for basic scanning and is the cheapest vulnerability
  # signal available. There is no reason to have it off.
  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  # Production repositories should not vanish because a plan destroyed the
  # stack; the images are the rollback path.
  force_delete = var.environment != "prod"

  tags = merge(var.tags, { Name = var.repository_name })
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  # Rules are evaluated in ascending rulePriority and each image is acted on by
  # the first rule that matches. Untagged images are expired aggressively;
  # release-tagged images are kept by count so a rollback target always exists.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after ${var.untagged_retention_days} days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_retention_days
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the most recent ${var.tagged_image_count} release images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = var.retained_tag_prefixes
          countType     = "imageCountMoreThan"
          countNumber   = var.tagged_image_count
        }
        action = { type = "expire" }
      },
    ]
  })
}

# Deny any pull over plaintext. ECR is HTTPS in practice, but stating it makes
# the guarantee explicit rather than incidental.
resource "aws_ecr_repository_policy" "require_tls" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "ecr:*"
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}
