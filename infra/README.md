# Infrastructure (AWS, Terraform)

The app runs on one EC2 instance with `docker-compose.prod.yml`, behind CloudFront (see the root
README, "Decisions"). Terraform describes the AWS side in two stacks:

| stack | what | applied by |
| --- | --- | --- |
| `bootstrap/` | Terraform state bucket, GitHub OIDC provider, deploy role, cost budget | you, once |
| `app/` | network, EC2, CloudFront, ECR, backups bucket, SES identity, settings | you, when it changes |

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

   If the invite email or its link doesn't work, skip it: select the user → **Reset password →
   Generate a one-time password**. Then, in a private browser window, open the **access portal URL**
   (step 5, not the normal console sign-in page), sign in with the user's **username** and that
   password, and you'll be asked to set a new password and register MFA.
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
   `AWS_REGION` = `eu-west-2`. Two more come from the app stack in step 7.

The role ARN isn't a secret, but keeping it on the environment ties it to the approval gate.

## 6. Apply the app stack

What it creates (about 40 resources; each file in `app/` starts with a comment explaining its part):

- **Network** (`network.tf`): a VPC with one public subnet. The server's firewall admits only
  CloudFront, on port 3000. There is no SSH.
- **Server** (`server.tf`, `user-data.sh`): a t3a.small running Ubuntu 24.04, with a fixed public
  address (Elastic IP) and a separate encrypted **data volume** for the database. That volume is
  protected from `terraform destroy`. On first boot the server installs Docker and the AWS CLI.
  Its IAM role may pull the app's images, read `/places/prod/*` settings, write backups and send
  email from the verified address.
- **CloudFront** (`cdn.tf`): HTTPS on a `*.cloudfront.net` URL. Nothing is cached except Next.js's
  hashed `/_next/static` files.
- **ECR** (`registry.tf`): `places-backend` and `places-frontend`, keeping the last 10 images.
- **Backups bucket** (`storage.tf`): private and encrypted; dumps expire after 30 days.
- **Settings** (`config.tf`): SSM Parameter Store values under `/places/prod/`, including a
  generated database password and the CloudFront origin secret. **SES** sender identity.

```bash
cd infra/app
cp terraform.tfvars.example terraform.tfvars   # sender_email, osm_contact (gitignored)
cp backend.hcl.example backend.hcl             # bucket = the bootstrap's state_bucket output
terraform init -backend-config=backend.hcl
terraform plan                                 # read it before applying
terraform apply                                # CloudFront takes a few minutes
terraform output                               # the URL, instance ID, ECR repositories
```

Then:

1. **Verify the sender address:** SES emails `sender_email` a link; click it.
2. **Add the Anthropic API key.** Terraform deliberately doesn't manage it, so it never lands in
   Terraform state. `read -s` keeps it off your screen and out of shell history:

   ```bash
   read -rsp "Anthropic API key: " KEY && echo && \
     aws ssm put-parameter --name /places/prod/ANTHROPIC_API_KEY --type SecureString --value "$KEY" && \
     unset KEY
   ```

3. **Check the server** (optional). Install the
   [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html),
   then run `aws ssm start-session --target <instance_id>` and
   `sudo tail /var/log/app-setup.log`. It should end with `setup finished`.

The URL shows a CloudFront error page until the first deploy (step 7), because nothing is running
on the server yet.

**SES sandbox:** new accounts can only send to verified addresses. Verify the addresses you'll sign
in with (SES console → Identities), or request production access (SES console → Account
dashboard), which takes about a day.

## 7. Deploy

`.github/workflows/deploy.yml` deploys every commit that reaches `main`, once CI has passed on it,
and waits for your approval first (the `production` environment's required reviewer). Each deploy
runs these steps:

1. Builds the production images and pushes them to ECR, tagged with the commit SHA.
2. Sends the server an SSM Run Command, which runs `scripts/deploy-on-server.sh`. That script:
   - writes the env file from `/places/prod/*` (readable by root only)
   - pulls the images
   - runs migrations
   - runs `up -d --wait`, which waits until every service is healthy
3. Checks `/api/health` through CloudFront.

Set two more variables on the GitHub `production` environment, from `terraform output` in
`infra/app`: `APP_INSTANCE_ID` = `instance_id`, and `APP_URL` = `url`.

**First deploy:** GitHub → **Actions → Deploy → Run workflow**, then approve it. The job log
includes the server's output. When it's green, make yourself an admin. It creates your user if
needed and gives you chat access:

```bash
aws ssm start-session --target <instance_id>
sudo -i
cd /srv/places/app
docker compose -f docker-compose.prod.yml --env-file .env exec backend python -m app.cli make-admin you@example.com
```

Then sign in at the app URL. While SES is in the sandbox, the address must be verified in SES
(step 6).

**Roll back:** run the Deploy workflow by hand with an earlier commit SHA from `main`. Its images
are already in ECR (the last 10 are kept), so nothing is rebuilt. Migrations don't run backwards:
if a release changed the schema, check that the older code still works with it.

## Monthly cost (eu-west-2, on-demand, approximate)

| item | ~USD/month |
| --- | --- |
| t3a.small, standard CPU credits (no burst surcharges) | 15.50 |
| public IPv4 address (Elastic IP) | 3.65 |
| EBS gp3: 16 GB root + 8 GB data | 2.25 |
| CloudFront (always-free tier: 1 TB and 10M requests a month) | 0 |
| ECR images, S3 backups and state, SSM standard parameters | < 0.50 |
| **total** | **about 22** |

Figures are from AWS's public price list; check them with the
[AWS Pricing Calculator](https://calculator.aws/). The bootstrap's budget alarm emails you before
$25.

## Known limitations

- **CloudFront → server is plain HTTP.** Without a domain of our own, the server can't present a
  certificate CloudFront will accept. So the hop from CloudFront's edge to port 3000 is
  unencrypted, over the internet. It is fenced in two ways. The firewall only admits CloudFront's
  published origin-facing addresses, and every request must carry a secret `X-Origin-Verify`
  header that only our distribution adds (checked in `frontend/proxy.ts`). The alternatives cost
  more: a domain plus a certificate on the server, or CloudFront VPC origins with a NAT
  (see the root README, "Decisions").
- **One server, one Availability Zone.** If it fails, the app is down until it's replaced. The
  data volume and nightly S3 backups mean nothing is lost beyond the last backup.
- **CloudFront's default certificate** accepts older TLS versions for browsers. A custom domain
  would let us require TLS 1.2+.
