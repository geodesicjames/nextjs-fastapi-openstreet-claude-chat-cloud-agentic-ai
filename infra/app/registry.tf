# Image registry: one ECR repository per image. GitHub Actions pushes (the
# deploy role from the bootstrap stack may push to "places-*"), and the
# server pulls.

locals {
  images = ["backend", "frontend"]
}

resource "aws_ecr_repository" "app" {
  for_each = toset(local.images)
  name     = "${var.project}-${each.key}"

  # Deploys tag images with the git commit, so a tag always means the same
  # image and can't be overwritten.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true # basic vulnerability scan, free
  }
}

# Keep storage (and its cost) small: the last 10 images are plenty to roll back to.
resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
