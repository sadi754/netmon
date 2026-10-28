// NetMon relay for Cloudflare Workers + D1 - the same API as relay/public/api.php,
// for people without a PHP host. Free plan is plenty (one tiny request a minute).
//
//   POST /api/ingest   router -> relay   (Authorization: Bearer INGEST_TOKEN)
//   GET  /api/traffic  relay -> NetMon   (Authorization: Bearer READ_TOKEN)
//   GET  /api/status   relay -> NetMon   (Authorization: Bearer READ_TOKEN)
//   GET  /api/health   anyone: is the relay up?
//   GET  /setup        first time only: makes the two tokens and shows them once
//
// Needs one D1 database bound as "DB". The tokens live in the database (made by
// /setup), so nothing has to be typed into the dashboard; to start over with new
// tokens, run `DELETE FROM meta WHERE key LIKE '%_token'` in the D1 console.
//
// The router sends cumulative counters (they only grow), so a missed post loses
// nothing: the next one covers the gap. A restart of the router is detected here.

const KEEP_DAYS = 800;
const MAX_BODY = 4096;
const VALUES = ["rx", "tx", "drx", "dtx", "arx", "atx"];

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS counters (router TEXT NOT NULL, iface TEXT NOT NULL, ts INTEGER NOT NULL,
     uptime INTEGER NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, drx INTEGER NOT NULL, dtx INTEGER NOT NULL,
     arx INTEGER NOT NULL DEFAULT 0, atx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (router, iface))`,
  `CREATE TABLE IF NOT EXISTS traffic (id INTEGER PRIMARY KEY AUTOINCREMENT, router TEXT NOT NULL,
     iface TEXT NOT NULL, ts INTEGER NOT NULL, span INTEGER NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL,
     drx INTEGER NOT NULL, dtx INTEGER NOT NULL, arx INTEGER NOT NULL DEFAULT 0, atx INTEGER NOT NULL DEFAULT 0,
     reset INTEGER NOT NULL DEFAULT 0)`,
  `CREATE INDEX IF NOT EXISTS traffic_ts ON traffic (ts)`,
];

let schemaReady = false;

async function ensureSchema(db) {
  if (schemaReady) return;
  await db.batch(SCHEMA.map((sql) => db.prepare(sql)));
  schemaReady = true;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

const err = (message, status) => json({ error: message }, status);
const now = () => Math.floor(Date.now() / 1000);

function token() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function getTokens(db) {
  const rows = (await db.prepare(`SELECT key, value FROM meta WHERE key IN ('ingest_token', 'read_token')`).all()).results;
  return Object.fromEntries(rows.map((r) => [r.key, r.value]));
}

// Constant-time comparison, so the token can't be guessed from response times.
function same(a, b) {
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  return header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() : "";
}

// RouterOS uptime: "3w2d10:20:30" (terminal) or "3w2d10h20m30s" (newer); plain seconds too.
function parseUptime(value) {
  if (typeof value === "number" && Number.isInteger(value) && value >= 0) return value;
  if (typeof value !== "string") return null;
  if (/^\d+$/.test(value)) return Number(value);
  let m = value.match(/^(?:(\d+)w)?(?:(\d+)d)?(\d+):(\d\d):(\d\d)(?:\.\d+)?$/);
  if (!m) m = value.match(/^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/);
  if (!m || !value) return null;
  const [w, d, h, mi, s] = m.slice(1).map((x) => Number(x || 0));
  return w * 604800 + d * 86400 + h * 3600 + mi * 60 + s;
}

function counter(value) {
  if (Number.isInteger(value) && value >= 0) return value;
  if (typeof value === "string" && /^\d{1,18}$/.test(value)) return Number(value);
  return null;
}

const nameField = (v) => (typeof v === "string" && /^[\w .:@()\-]{1,64}$/u.test(v) ? v : null);

async function ingest(request, env, tokens) {
  if (!tokens.ingest_token || !same(tokens.ingest_token, bearer(request))) return err("unauthorized", 401);
  const raw = await request.text();
  if (raw.length > MAX_BODY) return err("body too large", 413);
  let input;
  try { input = JSON.parse(raw); } catch { return err("expected a JSON object", 400); }
  if (!input || typeof input !== "object") return err("expected a JSON object", 400);

  const router = nameField(input.router ?? "router"), iface = nameField(input.iface), uptime = parseUptime(input.uptime);
  const current = {};
  for (const key of VALUES) {
    const value = counter(input[key] ?? 0);
    if (value === null) return err(`bad value for ${key}`, 400);
    current[key] = value;
  }
  if (router === null || iface === null || uptime === null) return err("router, iface and uptime are required", 400);

  const db = env.DB, ts = now();
  const prev = await db.prepare("SELECT * FROM counters WHERE router = ? AND iface = ?").bind(router, iface).first();
  const statements = [];
  let stored = false;
  if (prev) {
    const rebooted = uptime < prev.uptime;
    let reset = rebooted;
    const delta = {};
    for (const key of VALUES) {
      if (rebooted || current[key] < prev[key]) { delta[key] = current[key]; reset = true; }  // restarted from zero
      else delta[key] = current[key] - prev[key];
    }
    if (VALUES.some((k) => delta[k] > 0)) {
      statements.push(db.prepare(`INSERT INTO traffic (router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(router, iface, ts, Math.max(1, ts - prev.ts),
        ...VALUES.map((k) => delta[k]), reset ? 1 : 0));
      stored = true;
    }
  }
  statements.push(db.prepare(`INSERT OR REPLACE INTO counters (router, iface, ts, uptime, rx, tx, drx, dtx, arx, atx)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(router, iface, ts, uptime, ...VALUES.map((k) => current[k])));
  if (Math.random() < 1 / 500) {  // now and then: drop very old rows
    statements.push(db.prepare("DELETE FROM traffic WHERE ts < ?").bind(ts - KEEP_DAYS * 86400));
  }
  await db.batch(statements);  // atomic
  return json({ ok: true, stored });
}

async function traffic(url, env) {
  const after = /^\d+$/.test(url.searchParams.get("after") || "") ? Number(url.searchParams.get("after")) : 0;
  const given = url.searchParams.get("limit") || "";
  const limit = /^\d+$/.test(given) ? Math.min(20000, Math.max(1, Number(given))) : 5000;
  // id > ? walks the primary key: D1 reads only the rows it returns.
  const rows = (await env.DB.prepare(`SELECT id, router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset FROM traffic
    WHERE id > ? ORDER BY id LIMIT ?`).bind(after, limit).all()).results;
  const fields = ["id", "router", "iface", "ts", "span", "rx", "tx", "drx", "dtx", "arx", "atx", "reset"];
  return json({ fields, rows: rows.map((r) => fields.map((f) => r[f])), more: rows.length === limit, server_time: now() });
}

async function status(env) {
  const db = env.DB;
  const routers = (await db.prepare(`SELECT router, iface, ts AS last_seen, uptime, rx, tx, drx, dtx, arx, atx
    FROM counters ORDER BY ts DESC`).all()).results;
  // MAX(id) and MIN(ts) come straight from indexes (one row read each). No COUNT(*): on D1's free plan
  // rows read are counted, and counting a year of rows every minute would use up the daily allowance.
  const last = await db.prepare("SELECT COALESCE(MAX(id), 0) AS id FROM traffic").first();
  const first = await db.prepare("SELECT MIN(ts) AS ts FROM traffic").first();
  return json({ routers, first: first.ts, last_id: last.id, server_time: now() });
}

function page(title, body) {
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex"><title>${title}</title>
<style>body{font:15px/1.55 system-ui,"Segoe UI",sans-serif;background:#0a0e14;color:#a7b2c3;margin:0;padding:40px 16px}
main{max-width:760px;margin:0 auto}h1{color:#e6edf6;font-weight:600}h2{color:#e6edf6;font-size:17px;margin:24px 0 8px}
.box{background:#111823;border:1px solid #222c3b;border-radius:10px;padding:16px 18px;margin:14px 0}.ok{color:#3fb950}
code,.token{font-family:Consolas,monospace}.token{display:block;background:#0a0e14;border:1px solid #2a3547;border-radius:6px;
padding:10px 12px;color:#e6edf6;word-break:break-all;user-select:all;margin:6px 0 2px}
button{background:#1f6feb;color:#fff;border:0;border-radius:8px;padding:10px 18px;font:inherit;cursor:pointer}
a{color:#58a6ff}</style></head><body><main><h1>${title}</h1>${body}</main></body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex" } });
}

async function setup(request, url, env, tokens) {
  const intro = `<p>Sets up this relay for <a href="https://github.com/sadi754/netmon">NetMon</a>: your MikroTik router
    reports its internet byte counters here every minute, and NetMon reads them back.</p>`;
  if (tokens.ingest_token && tokens.read_token) {
    return page("NetMon relay setup", `${intro}<div class="box"><b class="ok">This relay is already set up.</b>
      <p>The tokens were shown once, when it was set up. Lost them? In the Cloudflare dashboard open the D1 database's
      Console, run <code>DELETE FROM meta WHERE key LIKE '%_token'</code>, then open this page again for new ones.
      The recorded traffic stays.</p></div>`);
  }
  const pressed = request.method === "POST"
    && (await request.text().catch(() => "")).split("&").includes("go=1");  // the form's button, not any POST
  if (!pressed) {
    return page("NetMon relay setup", `${intro}<form method="post"><p>This makes two random tokens - one for the router
      (to send), one for NetMon (to read) - and keeps them in this relay's database.</p>
      <button name="go" value="1">Set up the relay</button></form>`);
  }
  const ingestToken = token(), readToken = token();
  // INSERT OR IGNORE: if two people press the button at once, only the first one's tokens count.
  await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO meta (key, value) VALUES ('ingest_token', ?)").bind(ingestToken),
    env.DB.prepare("INSERT OR IGNORE INTO meta (key, value) VALUES ('read_token', ?)").bind(readToken),
  ]);
  const saved = await getTokens(env.DB);
  if (saved.ingest_token !== ingestToken || saved.read_token !== readToken) return setup(new Request(url), url, env, saved);
  return page("NetMon relay setup", `<div class="box"><b class="ok">Done - the relay is ready.</b> Copy the two tokens now:
    they are shown only this once.</div>
    <h2>1. In NetMon</h2><div class="box">Router tab → <b>Through a relay</b>: Relay address
    <span class="token">${url.origin}</span>Read token<span class="token">${readToken}</span>then <b>Save</b> and <b>Test</b>.</div>
    <h2>2. The router script</h2><div class="box">In the same place, open <b>Router script</b>, enter your internet interface
    (e.g. <code>pppoe-out1</code>), optionally your domestic address list, and this <b>router token</b>:
    <span class="token">${ingestToken}</span>Press <b>Make script</b> and paste it into the router's terminal.</div>`);
}

export default {
  async fetch(request, env) {
    if (!env.DB) return err("No D1 database bound as DB - see relay/cloudflare/README.md.", 500);
    const url = new URL(request.url);
    try {
      await ensureSchema(env.DB);
      const tokens = await getTokens(env.DB);
      const path = url.pathname.replace(/\/+$/, "") || "/";
      if (path === "/setup") return setup(request, url, env, tokens);
      if (path === "/api/health") return json({ ok: true, server_time: now(), set_up: Boolean(tokens.read_token) });
      if (!tokens.read_token) return err("This relay is not set up yet - open /setup in a browser.", 503);
      if (path === "/api/ingest" && request.method === "POST") return await ingest(request, env, tokens);
      if (path === "/api/traffic" || path === "/api/status") {
        if (!same(tokens.read_token, bearer(request))) return err("unauthorized", 401);
        return path === "/api/traffic" ? await traffic(url, env) : await status(env);
      }
      if (path === "/") {
        return page("NetMon relay", `<p>This address collects traffic counters from a router for the NetMon app.
          There is nothing to see here.</p>`);
      }
      return err("not found", 404);
    } catch (e) {
      return err("relay error: " + (e && e.message ? e.message : String(e)), 500);
    }
  },
};
