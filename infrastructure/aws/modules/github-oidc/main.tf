# GitHub Actions -> AWS via OIDC federation.
#
# This module exists so that no AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY ever
# needs to be stored in GitHub. Actions presents a short-lived OIDC token, AWS
# validates it against GitHub's provider, and the workflow gets temporary
# credentials scoped to one role.
#
# ## The trust policy is the entire security boundary
#
# A trust policy that checks only `aud` and the provider will let *any* GitHub
# repository in the world assume this role. The `sub` condition is what binds
# it to this repository, and it must be written carefully:
#
#   repo:OWNER/REPO:ref:refs/heads/Master   one branch
#   repo:OWNER/REPO:environment:production  one GitHub environment
#   repo:OWNER/REPO:pull_request            any PR from the repo
#   repo:OWNER/REPO:*                       anything in the repo -- too broad
#                                           for a deploy role
#
# The roles here are restricted to GitHub *environments*, which is the
# strongest of these: GitHub environments carry their own required-reviewer
# and branch rules, so the approval gate lives in GitHub and the `sub` claim
# cannot be produced at all without passing it. A branch condition alone can be
# satisfied by anyone who can push to that branch.
#
# ## `subjects` is an OR, so adding one widens the role
#
# Every entry becomes a value in a single StringLike condition, and IAM matches
# a condition if *any* value matches. So a role listing both
# `environment:terraform-plan-prod` and `ref:refs/heads/Master` is assumable by
# any job on Master with no environment at all -- the environment gate is not
# an additional requirement, it is an alternative one, and the weakest entry in
# the list is the role's real boundary.
#
# Both stacks previously carried a branch subject next to the environment
# subject, which read like defence in depth and was the opposite. List the
# environment alone unless a workflow genuinely mints the other claim.

data "aws_caller_identity" "current" {}

# GitHub's OIDC provider is account-global, so an account that already has one
# must reuse it rather than create a second.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS has verified GitHub's certificate chain natively since 2023 and no
  # longer uses this list for validation, but the API still requires a
  # non-empty value. It is GitHub's intermediate thumbprint.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = var.tags
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_oidc_provider_arn

  # Everything the plan/deploy roles are allowed to touch is confined to this
  # account. Cross-account is not a capability this design wants.
  account_id = data.aws_caller_identity.current.account_id
}

data "aws_iam_policy_document" "assume" {
  for_each = var.roles

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # StringLike rather than StringEquals so an environment or branch pattern
    # can be expressed, but every entry is still anchored to
    # "repo:<owner>/<repo>:" by the module -- a caller cannot widen it to
    # another repository by supplying a wildcard.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for s in each.value.subjects : "repo:${var.github_repository}:${s}"]
    }
  }
}

resource "aws_iam_role" "this" {
  for_each = var.roles

  name                 = "${var.name_prefix}-gha-${each.key}"
  description          = each.value.description
  assume_role_policy   = data.aws_iam_policy_document.assume[each.key].json
  max_session_duration = each.value.max_session_seconds

  tags = merge(var.tags, { Name = "${var.name_prefix}-gha-${each.key}" })
}

resource "aws_iam_role_policy_attachment" "managed" {
  for_each = merge([
    for role_key, role in var.roles : {
      for arn in role.managed_policy_arns : "${role_key}:${arn}" => {
        role = role_key
        arn  = arn
      }
    }
  ]...)

  role       = aws_iam_role.this[each.value.role].name
  policy_arn = each.value.arn
}

resource "aws_iam_role_policy" "inline" {
  for_each = { for k, v in var.roles : k => v if v.inline_policy_json != null }

  name   = "${var.name_prefix}-gha-${each.key}"
  role   = aws_iam_role.this[each.key].id
  policy = each.value.inline_policy_json
}
