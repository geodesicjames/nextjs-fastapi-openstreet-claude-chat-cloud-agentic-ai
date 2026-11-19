# Infrastructure (AWS, Terraform)

The app runs on one EC2 instance with `docker-compose.prod.yml`, behind CloudFront (see the root
README, "Decisions"). Terraform describes the AWS side in two stacks:

| stack | what | applied by |
| --- | --- | --- |
| `bootstrap/` | Terraform state bucket, GitHub OIDC provider, deploy role, cost budget | you, once |
| `app/` (next PR) | network, EC2, CloudFront, ECR, backups, SES, parameters | you, when it changes |

Nobody stores long-lived AWS keys. You sign in with SSO, and GitHub Actions gets short-lived
credentials through OIDC. Claude (or any other helper) never needs your credentials: you run every
`terraform apply` yourself.

---

## 1. Secure the account (once, in the AWS console)

1. **Root user:** turn on MFA (account menu → Security credentials → Assign MFA device). Make sure
   the root user has **no access keys**. From here on, use the root user only for account-level
   tasks.
2. **Region:** this project uses **eu-west-2 (London)**. Pick it in the console's region menu.

## 2. Sign-in for the CLI: IAM Identity Center (SSO)

This gives you short-lived CLI credentials instead of access keys that never expire.

1. Console → **IAM Identity Center** → **Enable**. If asked, enable it with AWS Organizations. That
   creates an organization containing just your account, which is free.
2. **Users → Add user:** your name and email. Accept the invite email, set a password, and register
   an **MFA device** when prompted.
3. **Permission sets → Create:** predefined **AdministratorAccess**, with a session duration of
   4 hours.
4. **AWS accounts →** select your account → **Assign users or groups** → your user, with
   AdministratorAccess.
5. Note the **AWS access portal URL** on the Identity Center dashboard
   (`https://d-xxxxxxxxxx.awsapps.com/start`).

## 3. Install and configure the AWS CLI (WSL / Linux)

```bash
curl -sSfLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install && aws --version   # needs v2

aws configure sso
#   SSO session name:        places
#   SSO start URL:           <your access portal URL>
#   SSO region:              eu-west-2
#   SSO registration scopes: (press Enter)
#   -> a browser opens; approve, then pick your account and AdministratorAccess
#   Default client Region:   eu-west-2
#   CLI default output:      json
#   Profile name:            places-admin

aws sso login --profile places-admin       # whenever the session expires
export AWS_PROFILE=places-admin
aws sts get-caller-identity                # shows your account and the SSO role
```

Install **Terraform 1.10 or newer** (`terraform version`): https://developer.hashicorp.com/terraform/install

## 4. Apply the bootstrap

Costs about nothing. S3 for a few KB of state, plus IAM and OIDC (free); your first two budgets are
free too.

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars    # set budget_email (this file is gitignored)
terraform init
terraform plan        # read it: about 10 resources, all described in main.tf
terraform apply
```

Then move the bootstrap's own state into the bucket it just created, so it isn't only on your
laptop:

```bash
cp backend.hcl.example backend.hcl              # set bucket to the state_bucket output
# in versions.tf, uncomment:   backend "s3" {}
terraform init -migrate-state -backend-config=backend.hcl   # answer "yes"
rm -f terraform.tfstate terraform.tfstate.backup             # now safely in S3
```

## 5. GitHub: the production environment

The deploy role trusts only workflow jobs that run in this repository's **`production`**
environment.

1. GitHub → repository **Settings → Environments → New environment →** `production`.
2. **Required reviewers:** add yourself, so every deploy waits for your approval.
3. **Deployment branches:** *Selected branches* → `main`.
4. **Environment variables →** `AWS_DEPLOY_ROLE_ARN` = the `deploy_role_arn` output, and
   `AWS_REGION` = `eu-west-2`.

The role ARN isn't a secret, but keeping it on the environment ties it to the approval gate.
