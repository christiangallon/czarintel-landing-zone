###############################################################################
# Remote state — S3 with DynamoDB state locking
#
# The bucket and lock table are created once by scripts/bootstrap.sh
# (see `bootstrap_state_backend`). Each environment uses its own Terraform
# workspace (dev / stg / prod); S3 keys are automatically prefixed with
# `env:/<workspace>/` so state files never collide.
#
# Initialise with:
#   terraform init \
#     -backend-config="bucket=czarintel-tfstate-<account-id>" \
#     -backend-config="region=us-east-1"
###############################################################################

terraform {
  backend "s3" {
    key                  = "landing-zone/terraform.tfstate"
    region               = "us-east-1"
    dynamodb_table       = "czarintel-terraform-locks"
    encrypt              = true
    workspace_key_prefix = "env"
  }
}
