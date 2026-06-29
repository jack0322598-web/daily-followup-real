# Free deployment: CircleCI + Cloudflare Pages

GitHub stores the source. CircleCI restores the prior deployed archives, runs the pipeline each morning, uploads the public-only output to Cloudflare Pages, and reports success or failure to Slack.

## CircleCI schedule

Create a schedule trigger for the `main` branch with pipeline parameter `run_daily=true`.

- Start time: `22:00 UTC`
- Frequency: daily
- KST target: around `07:00` the following morning

CircleCI may apply a short scheduling delay.

The scheduled workflow is split into three jobs so each stage has its own runtime budget:

1. `daily-collect` restores the deployed state, renders the initial selection, and collects article bodies.
2. `daily-summarize` summarizes the selected articles.
3. `daily-deploy` renders, validates, deploys, and sends the success notification.

Collection and summarization use the `large` resource class. A separate failure-notification job runs when any stage fails or times out.

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
- `SITE_URL` (for example `https://daily-followup.pages.dev`)
- `MAX_BACKFILL_DAYS=7`

The production CI config pins `SITE_URL` to the live Pages domain and requires the deployed state to be available. This prevents a transient sync failure from replacing the site with an incomplete archive set.

## Cloudflare token permissions

Create an API token limited to the account with `Cloudflare Pages: Edit` permission.

## Manual verification

Trigger a CircleCI pipeline with `run_daily=true`. To backfill one specific date, also set `news_date=YYYY-MM-DD`. Backfills must be dispatched one date per pipeline. A successful run must satisfy all four checks:

1. CircleCI job is green.
2. Cloudflare Pages production deployment is active.
3. The next run can restore generated archives and caches from the deployed site.
4. Slack receives the success message and site link.
