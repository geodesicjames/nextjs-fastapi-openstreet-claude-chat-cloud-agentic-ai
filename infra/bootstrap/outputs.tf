output "state_bucket" {
  description = "S3 bucket for Terraform state (use it in backend.hcl here and in infra/app)."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Role for GitHub Actions; store it as the AWS_DEPLOY_ROLE_ARN variable of the repository's production environment."
  value       = aws_iam_role.deploy.arn
}

output "region" {
  value = var.region
}
