const DEFAULT_REPOSITORY = "jack0322598-web/daily-followup-real";
const DEFAULT_WORKFLOW_FILE = "daily-update-v2.yml";
const DEFAULT_REF = "main";
const DEFAULT_TIMEZONE = "Asia/Seoul";

function json(data, init = {}) {
  return new Response(JSON.stringify(data, null, 2), {
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...(init.headers || {}),
    },
    ...init,
  });
}

function getConfig(env) {
  const repository = env.GITHUB_REPOSITORY || DEFAULT_REPOSITORY;
  const workflowFile = env.GITHUB_WORKFLOW_FILE || DEFAULT_WORKFLOW_FILE;
  const ref = env.GITHUB_REF || DEFAULT_REF;
  const timezone = env.TIMEZONE || DEFAULT_TIMEZONE;
  const token = env.GITHUB_TOKEN;
  if (!token) {
    throw new Error("Missing GITHUB_TOKEN secret.");
  }

  return { repository, workflowFile, ref, timezone, token };
}

function getKstDate(date = new Date()) {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: DEFAULT_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  return formatter.format(date);
}

function getYesterdayKst() {
  const now = new Date();
  const kstNow = new Date(now.toLocaleString("en-US", { timeZone: DEFAULT_TIMEZONE }));
  kstNow.setDate(kstNow.getDate() - 1);
  return getKstDate(kstNow);
}

async function dispatchWorkflow(env, newsDate = "") {
  const { repository, workflowFile, ref, token } = getConfig(env);
  const url = `https://api.github.com/repos/${repository}/actions/workflows/${workflowFile}/dispatches`;
  const body = { ref };

  if (newsDate) {
    body.inputs = { news_date: newsDate };
  }

  const response = await fetch(url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "content-type": "application/json",
      "user-agent": "news-scraper-dispatch-worker",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`GitHub dispatch failed (${response.status}): ${text}`);
  }

  return {
    ok: true,
    repository,
    workflowFile,
    ref,
    newsDate: newsDate || null,
  };
}

async function listRecentWorkflowRuns(env, event = "workflow_dispatch") {
  const { repository, workflowFile, ref, token } = getConfig(env);
  const url = new URL(
    `https://api.github.com/repos/${repository}/actions/workflows/${workflowFile}/runs`
  );
  url.searchParams.set("branch", ref);
  url.searchParams.set("event", event);
  url.searchParams.set("per_page", "3");

  const response = await fetch(url, {
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "user-agent": "news-scraper-dispatch-worker",
    },
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`GitHub run lookup failed (${response.status}): ${text}`);
  }

  const data = await response.json();
  return (data.workflow_runs || []).map((run) => ({
    id: run.id,
    run_number: run.run_number,
    event: run.event,
    status: run.status,
    conclusion: run.conclusion,
    created_at: run.created_at,
    html_url: run.html_url,
  }));
}

function isAuthorized(request, env) {
  const expected = env.MANUAL_TRIGGER_SECRET;
  if (!expected) {
    return false;
  }

  const authHeader = request.headers.get("authorization") || "";
  return authHeader === `Bearer ${expected}`;
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(dispatchWorkflow(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return json({
        ok: true,
        repository: env.GITHUB_REPOSITORY || DEFAULT_REPOSITORY,
        workflowFile: env.GITHUB_WORKFLOW_FILE || DEFAULT_WORKFLOW_FILE,
        ref: env.GITHUB_REF || DEFAULT_REF,
        timezone: env.TIMEZONE || DEFAULT_TIMEZONE,
        todayKst: getKstDate(),
        defaultNewsDate: getYesterdayKst(),
      });
    }

    if (request.method === "POST" && url.pathname === "/manual") {
      if (!isAuthorized(request, env)) {
        return json({ ok: false, error: "Unauthorized." }, { status: 401 });
      }

      const requestedDate = url.searchParams.get("date") || "";
      const result = await dispatchWorkflow(env, requestedDate);
      const recentRuns = await listRecentWorkflowRuns(env);
      return json({
        ...result,
        recentRuns,
      });
    }

    return json(
      {
        ok: false,
        error: "Not found.",
        supportedRoutes: [
          "GET /health",
          "POST /manual?date=YYYY-MM-DD",
        ],
      },
      { status: 404 }
    );
  },
};
