# CloudFront: the public HTTPS front door, at its own *.cloudfront.net URL.
#
# Browser --HTTPS--> CloudFront --HTTP:3000--> Next.js on the server
#
# The second hop is plain HTTP over the internet, because without a domain
# of our own the server can't get a certificate CloudFront would accept. It
# is limited by (1) the server's firewall, which admits only CloudFront's
# addresses, and (2) a secret header our distribution adds, which the app
# checks (frontend/proxy.ts). See infra/README.md, "Known limitations".

resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

# AWS-managed policies, looked up by name.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Forward everything the viewer sent (cookies, query strings, headers),
# including Host, so Next.js sees the CloudFront hostname and its
# same-origin checks on Server Actions and API routes pass.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

# HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, etc.
data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

resource "aws_cloudfront_distribution" "app" {
  enabled         = true
  comment         = var.project
  is_ipv6_enabled = true
  http_version    = "http2and3"
  # North America and Europe edge locations only: the cheapest price class.
  price_class = "PriceClass_100"

  origin {
    origin_id   = "app"
    domain_name = aws_eip.app.public_dns

    custom_origin_config {
      http_port              = 3000
      https_port             = 443 # unused
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # Chat replies stream as server-sent events; a tool call can leave a
      # gap of several seconds between events. 60 s is the maximum.
      origin_read_timeout = 60
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  # Everything is dynamic by default: per-user pages, the API, the chat stream.
  default_cache_behavior {
    target_origin_id           = "app"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  # Next.js build output: file names contain a content hash, so cache hard.
  ordered_cache_behavior {
    path_pattern               = "/_next/static/*"
    target_origin_id           = "app"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
