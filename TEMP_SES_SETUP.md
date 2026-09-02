# TEMPORARY — Amazon SES setup instructions (delete this file when done)

This file is a hand-off prompt for a Claude Code session running on a machine
that has the AWS CLI configured with admin (or IAM+SES-capable) credentials,
and access to the Ndiro `.env` file. It is checked in temporarily so it rides
the native-accounts PR; it contains **no secrets** and should be **deleted
from the branch once the setup is done**.

---

## Prompt for the laptop Claude session

You are setting up Amazon SES for **Ndiro** (this repo), which just gained
native email/password accounts. The app sends three kinds of plaintext mail
via the SES v2 API (`mailer.py`, boto3 `sesv2:SendEmail`): email-verification
links, password-reset links, and "you already have an account" notices.

The app reads these env vars (see `env_template.txt`):

- `MAIL_FROM` — the sender, must be an SES-verified identity
  (e.g. `Ndiro <no-reply@example.com>`). Unset = email features disabled.
- `SES_REGION` — only if SES runs in a different region than `AWS_REGION`.
- `APP_BASE_URL` — absolute base for the links inside emails
  (e.g. `https://ndiro.example.com`). Strongly recommended in production.

The app already has AWS credentials in `.env` (`AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`) that it uses for DynamoDB and S3 — SES uses the same
credential chain, so **no new keys are needed**, only permissions.

### Ground rules

- **Never print, log, or commit secret values** (access keys, `.env`
  contents). Refer to keys by their IAM user name / key ID only.
- `.env` is git- and docker-ignored; edit it in place, don't copy it around.
- Prefer least privilege for the app's IAM principal: `ses:SendEmail` only.

### Steps

1. **Find the app's IAM principal and region.** Read `AWS_REGION` and the
   access key ID from `.env` (do not echo the secret), then map the key to
   its IAM user: `aws sts get-caller-identity` with the app's profile, or
   `aws iam list-access-keys --user-name <candidate>` from the admin profile.

2. **Verify a sender identity** in the chosen SES region:
   - If the operator owns a domain (best deliverability):
     `aws sesv2 create-email-identity --email-identity example.com` and add
     the returned DKIM CNAME records to DNS; wait for VERIFIED.
   - Otherwise a single address works:
     `aws sesv2 create-email-identity --email-identity no-reply@example.com`
     (SES mails that address a verification link — the operator must click it;
     for an address identity use a real inbox they control).
   - Check with `aws sesv2 get-email-identity --email-identity ...`.

3. **Check the SES sandbox.** `aws sesv2 get-account` — if
   `ProductionAccessEnabled` is false, mail can only go TO verified
   addresses. For a ~100-user instance the operator should request production
   access (SES console → Account dashboard → Request production access; this
   is a manual form — tell the operator what to write: transactional
   account-verification/password-reset mail, low volume, no marketing).
   Until then, verify the operator's own test address(es) the same way as
   step 2 so the flow can be tested end to end.

4. **Grant the app permission to send.** Attach an inline policy to the
   app's IAM user (the one whose keys are in `.env`):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": "ses:SendEmail",
       "Resource": "*"
     }]
   }
   ```

   (Optionally scope `Resource` to the identity ARN from step 2.)

5. **Update `.env`** (and the production host's env if it's deployed, e.g.
   Render's environment settings):

   ```
   MAIL_FROM=Ndiro <no-reply@example.com>   # the verified identity
   # SES_REGION=us-east-1                   # only if different from AWS_REGION
   APP_BASE_URL=https://your-deployed-host  # what the emailed links start with
   ```

6. **Test.**
   - CLI smoke test with the APP's credentials (proves the IAM policy):
     `aws sesv2 send-email --from-email-address "<MAIL_FROM>"
     --destination ToAddresses=<a verified test address>
     --content 'Simple={Subject={Data=Ndiro SES test},Body={Text={Data=hello}}}'`
   - App-level: start the dev server (`python app.py`), open `/status` —
     the Email row must show **enabled** — then walk `/signup` with a test
     address and confirm the verification mail arrives and the link works.
     A send failure prints one `MAIL_ERROR <type> <code>` line on stdout.

7. **Report back**: identity + verification state, sandbox state, which IAM
   user got the policy, which env vars were set where — again, no secret
   values.

### Cleanup

When everything works, **delete this file** (`TEMP_SES_SETUP.md`) from the
branch/PR — it was only ever a hand-off note.
