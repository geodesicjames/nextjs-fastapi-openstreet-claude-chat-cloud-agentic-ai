terraform {
  # 1.10+ for S3 state locking without DynamoDB (use_lockfile).
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
  }

  # The bootstrap creates the state bucket, so its own state starts local:
  # in a brand-new account, comment this out for the first apply, then move
  # the state into the bucket (see README.md, step 4):
  #   terraform init -migrate-state -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}
