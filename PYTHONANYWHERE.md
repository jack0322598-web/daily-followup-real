# PythonAnywhere deployment

This project is deployed as a generated static briefing served by a small WSGI application. A PythonAnywhere scheduled task runs the full pipeline every morning and sends success or failure notifications to Slack.

## Files used

- `deploy/pythonanywhere_daily.py`: safe scheduled runner, validation, rollback, and Slack notification
- `deploy/pythonanywhere_wsgi.py`: serves `index.html`, archives, JavaScript, JSON, and images
- `.env`: production secrets; ignored by Git

## Scheduled command

PythonAnywhere's task time is UTC. For 07:00 KST, schedule the task at 22:00 UTC on the previous day.

```bash
/home/YOUR_USERNAME/.virtualenvs/news-scraper/bin/python /home/YOUR_USERNAME/news_scraper/deploy/pythonanywhere_daily.py
```

## Useful checks

```bash
cd ~/news_scraper
~/.virtualenvs/news-scraper/bin/python deploy/pythonanywhere_daily.py --dry-run
~/.virtualenvs/news-scraper/bin/python deploy/pythonanywhere_daily.py --notify-test
```

To generate one explicit date:

```bash
~/.virtualenvs/news-scraper/bin/python deploy/pythonanywhere_daily.py --date 2026-06-19
```

The runner preserves the previous published page if generation or validation fails. Logs are written under `logs/`.
