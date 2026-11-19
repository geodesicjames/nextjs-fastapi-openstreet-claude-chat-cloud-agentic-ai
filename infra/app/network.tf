# Network: one VPC with one public subnet. The server has a public address so
# it can reach the internet (Anthropic, OpenStreetMap, image pulls) without a
# NAT gateway (~$37/month), but its firewall only lets in CloudFront.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  # The instance's public DNS name is CloudFront's origin, so it needs one.
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.project }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.project }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "${var.project}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.project}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Take over the VPC's default security group and leave it with no rules, so
# nothing that falls back to it is reachable.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

# CloudFront's addresses for talking to origins, maintained by AWS.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "app" {
  name        = "${var.project}-app"
  description = "App server: Next.js reachable from CloudFront only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.project}-app" }
}

# Only CloudFront can reach Next.js. Those addresses are shared by all
# CloudFront customers, so the app also checks a secret header that only our
# distribution sends (frontend/proxy.ts). No SSH: admin access is through SSM.
# (This prefix list counts as ~55 rules towards the group's limit of 60.)
resource "aws_vpc_security_group_ingress_rule" "from_cloudfront" {
  security_group_id = aws_security_group.app.id
  description       = "Next.js from CloudFront"
  ip_protocol       = "tcp"
  from_port         = 3000
  to_port           = 3000
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

# Outbound: HTTPS to Anthropic, OpenStreetMap, ECR, SSM and S3; HTTP for the
# Ubuntu package mirrors.
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS out"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "http" {
  security_group_id = aws_security_group.app.id
  description       = "HTTP out (package mirrors)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}
