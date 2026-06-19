# Cloudflare Worker Dispatch Scheduler

This Worker replaces GitHub's unreliable `schedule` trigger for this repo.
Cloudflare runs the cron on its own infrastructure, then calls GitHub's
`workflow_dispatch` API for `daily-update-v2.yml`.

## What It Does

- Runs every day at `07:00 KST` via Cloudflare Cron Triggers.
- Calls GitHub Actions with `workflow_dispatch`.
- Lets you trigger the workflow manually through a protected HTTP endpoint.

## Files

- `wrangler.jsonc`: Worker config and cron schedule.
- `src/index.js`: Scheduled trigger and manual trigger endpoint.
- `.dev.vars.example`: Local secret variable example.

## One-Time Setup

1. Install Node.js and npm.
   Official docs: https://developers.cloudflare.com/workers/wrangler/install-and-update/
2. In this folder, install Wrangler:

```powershell
cd C:\Users\GRAM_\Desktop\news_scraper\cloudflare-dispatch-worker
npm install
```

3. Log in to Cloudflare:

```powershell
npx wrangler login
```

4. Add the GitHub token as a Worker secret.
   Use a GitHub token that can dispatch workflows for `jack0322598-web/daily-followup-real`.

```powershell
npx wrangler secret put GITHUB_TOKEN
```

5. Add a manual trigger secret for the `/manual` endpoint:

```powershell
npx wrangler secret put MANUAL_TRIGGER_SECRET
```

6. Deploy:

```powershell
npx wrangler deploy
```

## Manual Test

After deploy, call the Worker manually:

```powershell
curl -X POST "https://<your-worker-subdomain>.workers.dev/manual" `
  -H "Authorization: Bearer <MANUAL_TRIGGER_SECRET>"
```

To force a specific news date:

```powershell
curl -X POST "https://<your-worker-subdomain>.workers.dev/manual?date=2026-06-16" `
  -H "Authorization: Bearer <MANUAL_TRIGGER_SECRET>"
```

Health check:

```powershell
curl "https://<your-worker-subdomain>.workers.dev/health"
```

## Important Notes

- Cloudflare cron expressions are UTC. `0 22 * * *` means `07:00 KST`.
- Do not store `GITHUB_TOKEN` in `wrangler.jsonc`. Keep it as a secret.
- Once Cloudflare deployment is verified, disable the local Windows task to avoid duplicate runs:

```powershell
Disable-ScheduledTask -TaskName "NewsScraperDailyUpdate"
```

## Reference Docs

- Cloudflare Cron Triggers:
  https://developers.cloudflare.com/workers/configuration/cron-triggers/
- Wrangler config:
  https://developers.cloudflare.com/workers/wrangler/configuration/
- Wrangler secrets:
  https://developers.cloudflare.com/workers/configuration/secrets/
- GitHub workflow dispatch API:
  https://docs.github.com/rest/actions/workflows
