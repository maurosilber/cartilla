// Cloudflare Worker that files the corrections sent from index.html as GitHub issues,
// so that users don't need a GitHub account. Turnstile keeps bots out, and the GitHub
// token stays here. The issues then follow the same path as those opened on GitHub:
// the correction workflow checks them and opens a pull request.
//
// Deploy, from this folder:
//   npx wrangler deploy
//   npx wrangler secret put GITHUB_TOKEN      # fine-grained, this repo only, Issues: read and write
//   npx wrangler secret put TURNSTILE_SECRET  # from a Turnstile widget for ORIGIN's domain
// then fill in REPORT in index.html with the worker's URL and the Turnstile site key.

const MAX_TITLE = 200;
const MAX_BODY = 8000; // GitHub's new-issue links fit about as much

export default {
  async fetch(req, env) {
    const cors = {
      "Access-Control-Allow-Origin": env.ORIGIN,
      "Access-Control-Allow-Methods": "POST",
      "Access-Control-Allow-Headers": "Content-Type",
      Vary: "Origin",
    };
    const reply = (status, data) => Response.json(data, { status, headers: cors });
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (req.method !== "POST") return reply(405, { error: "método no permitido" });
    if (req.headers.get("Origin") !== env.ORIGIN) return reply(403, { error: "origen no permitido" });
    // Before Turnstile, so that a flood doesn't reach it.
    const ip = req.headers.get("CF-Connecting-IP") ?? "";
    if (!(await env.LIMITER.limit({ key: ip })).success) {
      return reply(429, { error: "enviaste otra corrección hace poco, probá de nuevo en unos segundos" });
    }

    let title, body, token;
    try {
      ({ title, body, token } = await req.json());
    } catch {
      return reply(400, { error: "JSON inválido" });
    }
    if (![title, body, token].every((s) => typeof s === "string" && s) ||
        title.length > MAX_TITLE || body.length > MAX_BODY) {
      return reply(400, { error: "corrección inválida" });
    }

    const form = new FormData();
    form.append("secret", env.TURNSTILE_SECRET);
    form.append("response", token);
    if (ip) form.append("remoteip", ip);
    const captcha = await (await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify",
      { method: "POST", body: form })).json();
    if (!captcha.success) return reply(403, { error: "no se pudo verificar que no seas un robot" });

    const res = await fetch(`https://api.github.com/repos/${env.REPO}/issues`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "cartilla-corrections",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ title, body }),
    });
    if (!res.ok) {
      console.error("GitHub", res.status, await res.text());
      return reply(502, { error: "GitHub no respondió" });
    }
    const issue = await res.json();
    return reply(201, { url: issue.html_url, number: issue.number });
  },
};
