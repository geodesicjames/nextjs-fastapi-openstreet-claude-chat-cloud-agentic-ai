terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # State lives in the bucket the bootstrap stack created. The bucket name
  # contains your account ID, so it comes from backend.hcl (gitignored):
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      # The deploy role may only run commands on instances tagged with this
      # Project (see infra/bootstrap/main.tf), so keep it in sync.
      Project   = var.project
      ManagedBy = "terraform"
      Stack     = "app"
    }
  }
}
