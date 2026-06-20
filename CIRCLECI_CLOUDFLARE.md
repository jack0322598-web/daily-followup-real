# Free deployment: CircleCI + Cloudflare Pages

GitHub stores the source and generated archives. CircleCI runs the pipeline each morning, uploads the public-only output to Cloudflare Pages, pushes generated archives back to GitHub, and reports success or failure to Slack.

## CircleCI schedule

Create a schedule trigger for the `main` branch with pipeline parameter `run_daily=true`.

- Start time: `22:00 UTC`
- Frequency: daily
- KST target: around `07:00` the following morning

CircleCI may apply a short scheduling delay.

## CircleCI environment variables

Configure these in a CircleCI context or the project environment variables:

- `GEMINI_API_KEY`
- `OPENAI_API_KEY` (optional fallback)
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `SLACK_WEBHOOK_URL`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_PAGES_PROJECT`
- `GH_PUSH_TOKEN` (fine-grained GitHub token with Contents read/write for this repository)
- `SITE_URL` (for example `https://daily-followup.pages.dev`)
- `MAX_BACKFILL_DAYS=7`

## Cloudflare token permissions

Create an API token limited to the account with `Cloudflare Pages: Edit` permission.

## Manual verification

Trigger a CircleCI pipeline with `run_daily=true`. A successful run must satisfy all four checks:

1. CircleCI job is green.
2. Cloudflare Pages production deployment is active.
3. Generated archives are committed back to `main` when new content exists.
4. Slack receives the success message and site link.
