output "url" {
  description = "The app's public URL."
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "instance_id" {
  description = "The app server, for SSM: aws ssm start-session --target <id>"
  value       = aws_instance.app.id
}

output "ecr_repositories" {
  description = "Where CI pushes images."
  value       = { for name, repo in aws_ecr_repository.app : name => repo.repository_url }
}

output "backups_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "ssm_prefix" {
  description = "Settings and secrets path in Parameter Store."
  value       = local.ssm_prefix
}
