# The app server: one EC2 instance running docker-compose.prod.yml.
#
# - Ubuntu 24.04 LTS (security updates install themselves), Docker from
#   Docker's own apt repository, and the AWS CLI, set up on first boot by
#   user-data.sh.
# - No SSH key and no port 22: shell access and deploys go through SSM.
# - The database lives on a separate EBS volume, so replacing the instance
#   (a new OS image, a different size) doesn't touch the data.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  data_dir   = "/srv/${var.project}"
}

# Canonical publishes the current Ubuntu image ID as a public SSM parameter.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.project}-data" }

  lifecycle {
    prevent_destroy = true # the database lives here
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  # Burstable instances default to "unlimited" on t3a, which bills extra when
  # the CPU stays busy. "standard" throttles instead: no surprise charges.
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
    # 2 hops so containers can use the instance role (the backend will send
    # email through SES with it); 1 would limit credentials to the host.
    http_put_response_hop_limit = 2
  }

  user_data = templatefile("${path.module}/user-data.sh", {
    # NVMe devices are named by volume ID without the dash.
    data_device = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${replace(aws_ebs_volume.data.id, "-", "")}"
    data_dir    = local.data_dir
  })

  tags = { Name = var.project }

  lifecycle {
    # A newer Ubuntu image shouldn't replace a running server on the next
    # apply, and user data only runs on first boot anyway. To move to a new
    # image, replace the instance on purpose (terraform apply -replace=...).
    ignore_changes = [ami, user_data]
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.app.id
}

# A fixed public address: CloudFront's origin is this address's DNS name, so
# it mustn't change when the instance is stopped or replaced.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id
  tags     = { Name = var.project }
}

# --- What the server may do in AWS ---------------------------------------------------

resource "aws_iam_role" "app" {
  name = "${var.project}-app-server"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.project}-app-server"
  role = aws_iam_role.app.name
}

# Lets SSM manage the instance (Session Manager shell, Run Command deploys).
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "app" {
  name = "app"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # this action has no resource-level scoping
      },
      {
        Sid      = "PullAppImages"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [for repo in aws_ecr_repository.app : repo.arn]
      },
      {
        # Settings and secrets, written to the server's env file at deploy.
        # SecureStrings use the AWS-managed aws/ssm key, which needs no extra
        # KMS permission.
        Sid      = "ReadAppSettings"
        Effect   = "Allow"
        Action   = ["ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}*"
      },
      {
        Sid      = "WriteAndRestoreBackups"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.backups.arn}/*"
      },
      {
        Sid      = "ListBackups"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.backups.arn
      },
      {
        Sid      = "SendSignInEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = aws_sesv2_email_identity.sender.arn
      },
    ]
  })
}
