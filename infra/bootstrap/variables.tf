variable "region" {
  description = "AWS region for everything the project creates."
  type        = string
  default     = "eu-west-2" # London
}

variable "project" {
  description = "Short name used in resource names and the Project tag."
  type        = string
  default     = "places"
}

variable "github_subject_prefix" {
  description = <<-EOT
    How GitHub's OIDC tokens name the repository allowed to deploy: the start of their
    `sub` claim. This repository uses GitHub's immutable subjects, which include the owner's
    and the repository's numeric IDs as well as their names, so a deleted or renamed
    repository's name can't be reused to deploy. Look it up with:
      gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  type        = string
  default     = "repo:geodesicjames@29415856/nextjs-fastapi-openstreet-claude-chat-cloud-agentic-ai@1461866251"
}

variable "budget_email" {
  description = "Where AWS Budgets sends cost alerts. Set it in terraform.tfvars (gitignored)."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget; alerts at 80% actual and 100% forecast."
  type        = number
  default     = 25
}
