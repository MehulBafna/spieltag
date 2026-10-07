# Spieltag

A self-evaluating Bundesliga forecasting pipeline on AWS. It predicts every matchday, freezes the
predictions before kickoff, scores itself against the real results and publishes the track record.

**Status: week 1 of v1.** The foundation and the data ingestion exist. Models, the Lambda, the
schedule and the dashboard come in the next weeks.

| Done | Not yet |
| --- | --- |
| OpenLigaDB client with retries | Elo baseline and features (week 2) |
| Data contracts (pandera) | Walk-forward evaluation (week 2) |
| Raw snapshots and curated Parquet, local or S3 | Lambda, EventBridge, CloudFront (week 3) |
| Terraform: state bucket, GitHub OIDC roles, budget alert, ECR, data lake bucket | CloudWatch alarms (week 3) |
| CI: ruff, pytest, terraform fmt and validate | Deploy workflow (week 3) |

Data: results and fixtures come from [OpenLigaDB](https://www.openligadb.de), licensed under the ODbL.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest

# fetch one season, validate it, write raw JSON and a curated Parquet file under ./data
spieltag-ingest --season 2025
```

If validation fails, the raw answer is still saved under `data/raw/`, nothing curated changes and the
command exits with code 2. A problem reaching the API exits with code 3.

**Check the first live run.** The parser was written from the documented shape of the API. The test
fixture is invented, and it could not be compared with a live answer while this was written. If
`spieltag-ingest` reports a missing field, open the saved raw JSON, compare the field names with
`src/spieltag/ingest.py`, and fix the parser and the fixture together.

## Set up AWS

You need an AWS account, the AWS CLI, Terraform 1.11 or newer, and an empty GitHub repository named
like the one in your `terraform.tfvars`.

**Do not use root access keys.** Create an admin user in IAM Identity Center (or an IAM user with MFA),
sign in with `aws configure sso` and check `aws sts get-caller-identity` before you start.

```bash
# 1. Bootstrap, once, with local state
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars   # set github_owner, github_repo, alert_email
terraform fmt -recursive ..                    # one-time tidy before the first commit
terraform init
terraform apply
cd ../..

# 2. Main stack, with state in the new bucket
terraform -chdir=infra/main init \
  -backend-config="bucket=$(terraform -chdir=infra/bootstrap output -raw state_bucket)" \
  -backend-config="region=eu-central-1"
terraform -chdir=infra/main apply

# 3. First ingest straight into S3
spieltag-ingest --season 2025 --bucket "$(terraform -chdir=infra/main output -raw lake_bucket)"
```

Then, in the GitHub repository settings, add two repository **variables** (not secrets) from the
bootstrap outputs: `AWS_DEPLOY_ROLE_ARN` and `AWS_PLAN_ROLE_ARN`. The deploy workflow in week 3 uses them.

The bootstrap state file stays on your computer (`infra/bootstrap/terraform.tfstate`, ignored by git).
It describes only the bootstrap resources. Keep a copy somewhere safe.

## Cost and safety

- The budget alert is created first. It emails you at 80 percent of the actual monthly limit and at
  100 percent of the forecast. It ignores credits on purpose, so it still fires while credits pay the bill.
- If your account is on the AWS Free plan, upgrade to the Paid plan before the plan ends, otherwise the
  account closes with everything in it. The Billing console shows the date.
- `terraform destroy` in `infra/main` removes the stack. Empty the lake bucket first, versioned
  buckets cannot be deleted while they hold objects.

## Known limits of the deploy role

The deploy role can only touch resources named `spieltag-...`, with three exceptions that AWS cannot
limit by name:

- **CloudFront** actions apply to all distributions in the account.
- **Inline IAM policies** on roles named `spieltag-app-...` can be written freely, so the role could
  grant itself more permissions than it needs. Tighten this with a permissions boundary before you make
  the repository public or add collaborators.
- **ReadOnlyAccess** is attached to both roles so Terraform can refresh state. It can read most of the account.

## Layout

```
infra/bootstrap/   state bucket, GitHub OIDC roles, budget alert
infra/main/        ECR repository, data lake bucket (more each week)
src/spieltag/      ingest.py, contracts.py, store.py, pipeline.py, cli.py
tests/             pytest tests and an invented OpenLigaDB fixture
.github/workflows/ ci.yml
```
