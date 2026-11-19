# App settings and secrets, in SSM Parameter Store under /places/prod/.
#
# At deploy time the server reads everything under that path into a
# root-only env file for docker compose (parameter name = variable name).
#
# Terraform creates the values it knows or generates. Secrets it generates
# (the database password, the origin secret) are also in Terraform's state,
# which is why the state bucket is private and encrypted. The Anthropic API
# key is NOT managed here, so it never enters state: you add it yourself
# with the AWS CLI (see infra/README.md).

locals {
  ssm_prefix = "/${var.project}/prod/"
}

resource "random_password" "db" {
  length  = 32
  special = false # it goes inside DATABASE_URL, so keep it URL-safe
}

locals {
  settings = {
    FRONTEND_URL  = "https://${aws_cloudfront_distribution.app.domain_name}"
    OSM_CONTACT   = var.osm_contact
    POSTGRES_USER = var.project
    POSTGRES_DB   = var.project
    # Where the database's files go: the separate data volume.
    PGDATA_DIR = "${local.data_dir}/pgdata"
    # Where scripts/backup-db.sh uploads dumps.
    BACKUP_S3_URI = "s3://${aws_s3_bucket.backups.bucket}/db"
    EMAIL_BACKEND = "ses"
    EMAIL_FROM    = var.sender_email
    AWS_REGION    = var.region
  }
  secrets = {
    POSTGRES_PASSWORD    = random_password.db.result
    ORIGIN_VERIFY_SECRET = random_password.origin_verify.result
  }
}

resource "aws_ssm_parameter" "setting" {
  for_each = local.settings
  name     = "${local.ssm_prefix}${each.key}"
  type     = "String"
  value    = each.value
}

resource "aws_ssm_parameter" "secret" {
  # The values are secret, the names aren't; for_each needs plain keys.
  for_each = nonsensitive(toset(keys(local.secrets)))
  name     = "${local.ssm_prefix}${each.key}"
  type     = "SecureString"
  value    = local.secrets[each.key]
}

# --- Email (Amazon SES) -----------------------------------------------------------
# On apply, SES emails sender_email a verification link; click it before the
# app can send from that address. New accounts start in the SES "sandbox",
# where mail can only go to verified addresses; see infra/README.md.
resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.sender_email
}
