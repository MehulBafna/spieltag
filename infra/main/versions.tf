terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }

  # The bucket and region are passed at init time, see the README:
  #   terraform init -backend-config="bucket=<state bucket>" -backend-config="region=eu-central-1"
  backend "s3" {
    key          = "spieltag/main.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.project
      managed_by = "terraform"
      stack      = "main"
    }
  }
}
