# The public dashboard: CloudFront in front of the site/ folder of the lake bucket.
# The bucket itself stays private. Only CloudFront, through origin access control, can read site/.

locals {
  content_security_policy = join("; ", [
    "default-src 'self'",
    "img-src 'self' data:",
    "style-src 'self'",
    "script-src 'self'",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ])
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.project}-site"
  description                       = "Lets CloudFront read the site/ folder of the lake bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Honour the Cache-Control the pipeline writes (60 seconds on data files), within these limits.
resource "aws_cloudfront_cache_policy" "site" {
  name        = "${var.project}-site"
  min_ttl     = 0
  default_ttl = 300
  max_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true

    cookies_config {
      cookie_behavior = "none"
    }

    headers_config {
      header_behavior = "none"
    }

    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_response_headers_policy" "site" {
  name = "${var.project}-site-security"

  security_headers_config {
    content_security_policy {
      content_security_policy = local.content_security_policy
      override                = true
    }

    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
  }

  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "camera=(), microphone=(), geolocation=()"
      override = true
    }
  }
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  price_class         = "PriceClass_100"
  default_root_object = "index.html"
  comment             = "${var.project} dashboard"

  origin {
    domain_name              = aws_s3_bucket.lake.bucket_regional_domain_name
    origin_id                = "lake-site"
    origin_path              = "/site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id           = "lake-site"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.site.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.site.id
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
