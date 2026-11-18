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

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to deploy."
  type        = string
  default     = "geodesicjames/nextjs-fastapi-openstreet-claude-chat-cloud-agentic-ai"
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
