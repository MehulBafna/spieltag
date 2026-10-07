# Spieltag

A self-evaluating Bundesliga forecasting pipeline on AWS. It predicts every matchday, freezes the
predictions before kickoff, scores itself against the real results and publishes the track record.

**Status: week 2 of v1.** Ingestion, the models and the walk-forward evaluation exist and run on
your own machine or against the S3 data lake. The Lambda, the schedule and the dashboard come in
week 3.

| Done | Not yet |
| --- | --- |
| OpenLigaDB client with retries | Lambda, EventBridge, CloudFront (week 3) |
| Data contracts (pandera) for matches and predictions | Frozen pre-kickoff predictions (week 3) |
| Raw snapshots and curated Parquet, local or S3 | CloudWatch alarms (week 3) |
| Elo ratings and point-in-time features | Deploy workflow (week 3) |
| Three models: climatology, Elo ordered logit, feature logistic regression | Bookmaker benchmark, XGBoost, Dixon-Coles (v2) |
| Walk-forward evaluation with RPS, log loss, Brier and calibration | |
| Leakage tests, including a no-signal canary | |
| Terraform: state bucket, GitHub OIDC roles, budget alert, ECR, data lake bucket | |
| CI: ruff, pytest, terraform fmt and validate | |

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

The field names, the result types and every data contract rule were checked against live OpenLigaDB
answers for the 2025 and 2026 seasons on 7 October 2026. The test fixture is invented but has the same
shape. If the API ever changes, `spieltag-ingest` names the missing field. Compare it with
`src/spieltag/ingest.py` and fix the parser and the fixture together.

## Evaluate the models

```bash
# after ingesting 2016 to 2026 (see "Run it locally"), locally or with --bucket
spieltag-evaluate --first-season 2016 --out data
```

It replays history matchday by matchday, refits the three models before each one, scores every
prediction and writes `evaluation/bl1/report.md`, `report.json` and `predictions.parquet` next to the
data. The report holds the leaderboard, the score per season, paired comparisons with 95% intervals,
and a calibration table. **The numbers in a report are only meaningful for the data they ran on; none
are quoted here until they come from a real run.**

### Why the numbers can be trusted

- **Point in time.** The features of a match use only results that were known 24 hours before its
  own kickoff, which is when the production pipeline will freeze its predictions. A result counts as
  known 3 hours after its kickoff. Rest days come from the public schedule.
- **Walk forward.** Before every matchday the models are fitted only on results known 24 hours
  before that matchday's first kickoff. No model ever sees the matchday it predicts, let alone later
  ones.
- **Headline score.** Ranked probability score (lower is better), the standard for ordered football
  outcomes, next to log loss, Brier score and calibration. Every model is compared with climatology
  (how often home wins, draws and away wins happen), so skill is measured against something real.
- **No tuning.** Elo constants and model settings are fixed defaults. Tuning them on the same matches
  would flatter the result.
- **Tested for leakage.** `tests/test_features.py` changes results that were not yet known and checks
  that no earlier row moves, and that a match's own result never reaches its own features. A further
  test deliberately breaks the feature builder and checks that the leakage test notices.
  `tests/test_evaluate.py` runs the whole evaluation on a simulated league with no signal: if
  anything leaked, a model would beat the league averages there, and the test would fail.

Differences between models are reported with a 95% interval. If it contains zero, the data cannot
tell the two models apart, and the report says so.

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
src/spieltag/      ingest.py, contracts.py, store.py, pipeline.py, cli.py   (week 1)
                   ratings.py, features.py, models.py, metrics.py,          (week 2)
                   evaluate.py, evaluate_cli.py
tests/             pytest tests, an invented OpenLigaDB fixture and a simulated league
.github/workflows/ ci.yml
```
