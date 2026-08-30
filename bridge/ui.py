"""The page, as one string.

Deliberately one self-contained document: no bundler, no CDN, no font files.
The repo has no node in it, and a tool whose whole job is to fix a broken
network connection must not need the network to render. The
`Content-Security-Policy` the server sends (`default-src 'none'`, `connect-src
'self'`) is only honest because of that.

**The design is picone's**, which is opencode's v2 system: a primitive ramp,
semantic tokens on top of it, light by default and dark under
`[data-color-scheme]`. Ported as plain custom properties rather than reproduced
by eye, so the two stay recognisably the same product. Only the tokens this page
actually paints with are here; the ramps they resolve to are the originals.
Fonts fall back to `system-ui` / `ui-monospace` -- picone serves Inter and
JetBrains Mono locally, and shipping woff2 inside a Python string is not worth a
typeface.

**Three levels, each one click in:**

    gateways   the tunnels: state, ssh output, the auth prompt, add/edit
      jobs     that gateway's jobs, read through the tunnel
        events one job's event stream

Routing is the url fragment (`#/g/<gateway>`, `#/g/<gateway>/j/<job>`), so the
back button works and a page can be reloaded where it was. The token also
arrives in the fragment (`#token=...`) but is taken out of it once, on boot: a
fragment never reaches the server, so it stays out of access logs and caches on
the way in.
"""
from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-bridge</title>
<style>
/* ── Primitives: opencode v2 ramps, as picone carries them ── */
:root {
  --grey-50:#fff; --grey-100:#fafafa; --grey-200:#f2f2f2; --grey-300:#eee;
  --grey-400:#dbdbdb; --grey-500:#aeaeae; --grey-600:#808080; --grey-700:#5c5c5c;
  --grey-800:#3a3a3a; --grey-900:#2e2e2e; --grey-1000:#242424;
  --grey-1100:#161616; --grey-1200:#080808;
  --dark-20:#00000033; --dark-14:#00000024; --dark-12:#0000001f;
  --dark-10:#0000001a; --dark-8:#00000014; --dark-4:#0000000a;
  --light-20:#ffffff33; --light-16:#ffffff29; --light-10:#ffffff1a;
  --light-8:#ffffff14; --light-6:#ffffff0f;
  --red-100:#fcecebff; --red-300:#f2bbb7ff; --red-500:#f17471ff;
  --red-800:#b82d35ff; --red-900:#97252bff; --red-1200:#461516ff;
  --yellow-100:#fefaecff; --yellow-300:#f7e5b5ff; --yellow-500:#f2cf76ff;
  --yellow-800:#cb9f34ff; --yellow-900:#ac8833ff; --yellow-1200:#4b4025ff;
  --green-100:#e7f9eaff; --green-300:#b8e9c1ff; --green-500:#6bd586ff;
  --green-800:#198b43ff; --green-900:#1d783cff; --green-1200:#14361dff;
  --blue-100:#ecf1feff; --blue-300:#c3d4fdff; --blue-400:#a2bcffff;
  --blue-500:#7698fdff; --blue-600:#3b5cf6ff; --blue-700:#3250dfff;
  --blue-800:#2c47c8ff; --blue-900:#263fa9ff; --blue-1200:#1b2852ff;
  --purple-400:#9e99f7ff; --purple-700:#623be2ff;
}
/* ── Semantic tokens: light ── */
:root, [data-color-scheme="light"] {
  color-scheme: light;
  --bg-base:var(--grey-50); --bg-deep:var(--grey-100);
  --bg-layer-01:var(--grey-100); --bg-layer-02:var(--grey-200);
  --bg-layer-03:var(--grey-300);
  --text-base:var(--grey-1100); --text-muted:var(--grey-700);
  --text-faint:var(--grey-600); --text-accent:var(--blue-600);
  --border-muted:var(--dark-8); --border-base:var(--dark-10);
  --border-strong:var(--dark-20); --border-focus:var(--blue-500);
  --hover:var(--dark-4); --pressed:var(--dark-8);
  --bg-success:var(--green-100); --fg-success:var(--green-800); --bd-success:var(--green-300);
  --bg-warning:var(--yellow-100); --fg-warning:var(--yellow-800); --bd-warning:var(--yellow-300);
  --bg-danger:var(--red-100); --fg-danger:var(--red-800); --bd-danger:var(--red-300);
  --bg-info:var(--blue-100); --fg-info:var(--blue-800); --bd-info:var(--blue-300);
  --bg-magic:color-mix(in srgb, var(--purple-400) 16%, transparent);
  --fg-magic:var(--purple-700);
  --raised: 0 2px 4px 0 var(--dark-4), 0 1px 2px -1px var(--dark-8),
            0 0 0 .5px var(--dark-12);
}
[data-color-scheme="dark"] {
  color-scheme: dark;
  --bg-base:var(--grey-1100); --bg-deep:var(--grey-1200);
  --bg-layer-01:var(--grey-1000); --bg-layer-02:var(--grey-900);
  --bg-layer-03:var(--grey-800);
  --text-base:var(--grey-100); --text-muted:var(--grey-500);
  --text-faint:var(--grey-600); --text-accent:var(--blue-400);
  --border-muted:var(--light-8); --border-base:var(--light-10);
  --border-strong:var(--light-20); --border-focus:var(--blue-500);
  --hover:var(--light-6); --pressed:var(--light-10);
  --bg-success:var(--green-1200); --fg-success:var(--green-500); --bd-success:var(--green-900);
  --bg-warning:var(--yellow-1200); --fg-warning:var(--yellow-500); --bd-warning:var(--yellow-900);
  --bg-danger:var(--red-1200); --fg-danger:var(--red-500); --bd-danger:var(--red-900);
  --bg-info:var(--blue-1200); --fg-info:var(--blue-500); --bd-info:var(--blue-900);
  --bg-magic:color-mix(in srgb, var(--purple-700) 32%, transparent);
  --fg-magic:var(--purple-400);
  --raised: 0 2px 4px 0 #0000004d, 0 1px 2px 0 #0000004d, 0 0 0 .5px var(--light-16);
}
:root {
  --sans: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Consolas, monospace;
}
* { box-sizing:border-box; -webkit-font-smoothing:antialiased; }
body { margin:0; background:var(--bg-deep); color:var(--text-base);
  font:13px/1.5 var(--sans); }
a { color:var(--text-accent); text-decoration:none; }

/* ── Chrome ── */
header { position:sticky; top:0; z-index:5; display:flex; align-items:center;
  gap:8px; padding:9px 16px; background:var(--bg-base);
  border-bottom:1px solid var(--border-base); }
.crumbs { display:flex; align-items:center; gap:6px; min-width:0; flex:1; }
.crumbs button { background:none; border:none; padding:2px 4px; font:inherit;
  color:var(--text-muted); cursor:pointer; border-radius:4px; }
.crumbs button:hover { background:var(--hover); color:var(--text-base); }
.crumbs .here { color:var(--text-base); font-weight:500;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.crumbs .sep { color:var(--text-faint); }
main { padding:14px 16px 40px; max-width:940px; margin:0 auto; }
.section { margin:0 0 8px; font-size:11px; text-transform:uppercase;
  letter-spacing:.06em; color:var(--text-faint); }

/* ── Rows: picone's session-row idiom ── */
.list { display:flex; flex-direction:column; gap:6px; }
/* One card per thing. Its row is always there; its detail is nested inside the
   same border, because a panel below a separate border reads as a second,
   unrelated item. */
.card { background:var(--bg-base); border:1px solid var(--border-muted);
  border-radius:8px; overflow:hidden; }
.card .detail { border-top:1px solid var(--border-muted);
  padding:9px 11px 11px; background:var(--bg-deep); }
.item { display:flex; align-items:center; gap:9px; border-radius:6px;
  padding:8px 10px; }
.item.click { cursor:pointer; }
.item.click:hover { background:var(--hover); }
/* Actions sit on the row and stay out of the way until wanted -- picone
   reveals them on hover, which is the same move. */
.card .actions.onrow { opacity:0; transition:opacity .1s; }
.card:hover .actions.onrow, .card:focus-within .actions.onrow { opacity:1; }
@media (hover: none) { .card .actions.onrow { opacity:1; } }
.item .body { flex:1; min-width:0; display:flex; flex-direction:column; gap:1px; }
.head { display:flex; align-items:center; gap:6px; min-width:0; }
.title { font-size:12.5px; font-weight:500; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.sub { font-size:11.5px; color:var(--text-muted); overflow-wrap:anywhere; }
.meta { font-size:11px; color:var(--text-faint); }
.mono { font-family:var(--mono); }
.chev { color:var(--text-faint); flex:none; }
.actions { display:flex; gap:3px; flex:none; }

/* ── Tags and pills ── */
.tag { flex:none; display:inline-flex; align-items:center; gap:3px;
  padding:0 5px; border-radius:3px; font-size:9.5px; text-transform:uppercase;
  letter-spacing:.05em; background:var(--bg-layer-02); color:var(--text-muted); }
.tag.success { background:var(--bg-success); color:var(--fg-success); }
.tag.warning { background:var(--bg-warning); color:var(--fg-warning); }
.tag.danger  { background:var(--bg-danger);  color:var(--fg-danger); }
.tag.info    { background:var(--bg-info);    color:var(--fg-info); }
.tag.magic   { background:var(--bg-magic);   color:var(--fg-magic); }
.dot { width:6px; height:6px; border-radius:50%; flex:none;
  background:var(--text-faint); }
.dot.success{background:var(--fg-success)} .dot.warning{background:var(--fg-warning)}
.dot.danger{background:var(--fg-danger)}  .dot.info{background:var(--fg-info)}

/* ── Buttons ── */
button.btn { font:inherit; font-size:12px; padding:3px 9px; border-radius:6px;
  cursor:pointer; border:1px solid var(--border-base);
  background:var(--bg-base); color:var(--text-base); box-shadow:var(--raised); }
button.btn:hover { background:var(--hover); }
button.btn.primary { color:var(--text-accent); border-color:var(--blue-400); }
button.btn.danger { color:var(--fg-danger); border-color:var(--bd-danger); }
button.btn:disabled { opacity:.45; cursor:default; box-shadow:none; }

/* ── Panels ── */
.panel { background:var(--bg-base); border:1px solid var(--border-muted);
  border-radius:8px; padding:11px 12px; margin-top:10px; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px;
  font-size:11.5px; }
.kv dt { color:var(--text-faint); }
.kv dd { margin:0; overflow-wrap:anywhere; }
pre.console { margin:8px 0 0; padding:9px 10px; background:var(--bg-deep);
  border:1px solid var(--border-muted); border-radius:6px;
  font:11.5px/1.55 var(--mono); max-height:260px; overflow:auto;
  white-space:pre-wrap; overflow-wrap:anywhere; }
pre.console .cmd { color:var(--text-faint) }
pre.console .prompt { color:var(--fg-warning) }
pre.console .note { color:var(--text-faint); font-style:italic }
.ask { margin-top:8px; padding:10px; border:1px solid var(--bd-warning);
  background:var(--bg-warning); border-radius:8px; }
.ask p { margin:0 0 7px; font:11.5px/1.5 var(--mono); color:var(--fg-warning); }
.ask .row { display:flex; gap:6px; }
input, select { font:inherit; font-size:12px; padding:5px 8px; border-radius:6px;
  border:1px solid var(--border-base); background:var(--bg-base);
  color:inherit; width:100%; }
input:focus, select:focus { outline:2px solid var(--border-focus);
  outline-offset:-1px; }
label { display:block; font-size:11px; color:var(--text-faint); margin:8px 0 3px; }
form.edit { display:none } form.edit.open { display:block }
.err { color:var(--fg-danger); font-size:11.5px; }
.hint { color:var(--text-faint); font-size:11px; margin:5px 0 0; }
.empty { color:var(--text-faint); font-size:12px; padding:16px 2px; }

/* ── Events ── */
.ev { display:flex; gap:8px; padding:5px 9px; border-radius:6px;
  border:1px solid transparent; }
.ev:hover { background:var(--hover); }
/* "+00:04:12" is the useful reading, so the column is sized for it. The
   sequence number is still there, in the title. */
.ev .at { flex:none; width:66px; text-align:right; font:11px/1.6 var(--mono);
  color:var(--text-faint); font-variant-numeric:tabular-nums; }
.ev .kind { flex:none; width:82px; }
.ev .text { flex:1; min-width:0; font:11.5px/1.55 var(--mono);
  white-space:pre-wrap; overflow-wrap:anywhere; }
.ev.assistant .text { font-family:var(--sans); font-size:12.5px; }
.ev.error .text { color:var(--fg-danger); }
.ev .text.clamp { display:-webkit-box; -webkit-box-orient:vertical;
  -webkit-line-clamp:6; line-clamp:6; overflow:hidden; }
#gate { max-width:520px; margin:60px auto; padding:0 16px; }
#gate h1 { font-size:15px; }
</style>

<div id="gate" hidden>
  <h1>Token needed</h1>
  <p class="sub">Open the url <span class="mono">ab-bridge</span> printed, or
    paste its token. It stays in this tab.</p>
  <input id="gate-token" type="password" placeholder="bearer token" autofocus>
  <p><button class="btn primary" id="gate-go">Connect</button></p>
  <p class="err" id="gate-err"></p>
</div>

<div id="app" hidden>
  <header>
    <nav class="crumbs" id="crumbs"></nav>
    <span class="tag" id="link">…</span>
  </header>
  <main id="view"></main>
</div>

<script>
(function () {
  "use strict";
  var KEY = "agent-bridge-ui-token";
  var token = "";
  var st = { local: null, jobs: null, job: null, events: null, error: "" };
  var open = { out: {}, edit: {}, add: false };
  var route = { level: "gateways", gateway: "", job: "" };
  var timer = null;

  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  function paint() {
    document.documentElement.setAttribute(
      "data-color-scheme", dark.matches ? "dark" : "light");
  }
  paint();
  dark.addEventListener("change", paint);

  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function api(method, path, body) {
    return fetch(path, {
      method: method,
      headers: Object.assign({ Authorization: "Bearer " + token },
                             body ? { "Content-Type": "application/json" } : {}),
      body: body ? JSON.stringify(body) : undefined
    }).then(function (res) {
      if (res.status === 401) { throw new Error("unauthorized"); }
      return res.text().then(function (text) {
        var data = text ? JSON.parse(text) : {};
        if (!res.ok) {
          throw new Error((data.error && data.error.message) || res.statusText);
        }
        return data;
      });
    });
  }

  function ago(ts) {
    if (!ts) { return ""; }
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) { return Math.floor(s) + "s ago"; }
    if (s < 3600) { return Math.floor(s / 60) + "m ago"; }
    if (s < 86400) { return Math.floor(s / 3600) + "h ago"; }
    return Math.floor(s / 86400) + "d ago";
  }
  function stamp(value) {
    if (!value) { return ""; }
    var ts = typeof value === "number" ? value : Date.parse(value) / 1000;
    return isNaN(ts) ? String(value) : ago(ts);
  }

  // ── routing ──────────────────────────────────────────────────────────
  function readRoute() {
    var parts = (location.hash || "").replace(/^#\/?/, "").split("/");
    if (parts[0] === "g" && parts[1]) {
      route = { level: parts[2] === "j" && parts[3] ? "events" : "jobs",
                gateway: decodeURIComponent(parts[1]),
                job: parts[3] ? decodeURIComponent(parts[3]) : "" };
    } else {
      route = { level: "gateways", gateway: "", job: "" };
    }
  }
  function go(hash) { location.hash = hash; }

  // ── data ─────────────────────────────────────────────────────────────
  function load() {
    st.error = "";
    if (route.level === "gateways") {
      return api("GET", "/v1/state").then(function (d) { st.local = d; render(); });
    }
    var g = encodeURIComponent(route.gateway);
    // The local state comes along on every level: a job list that cannot load
    // is nearly always a tunnel that is down, and the answer to that belongs on
    // the same screen as the question.
    var local = api("GET", "/v1/state").then(function (d) { st.local = d; });
    if (route.level === "jobs") {
      return Promise.all([local,
        api("GET", "/v1/gateways/" + g + "/jobs?limit=50")
          .then(function (d) { st.jobs = d; })
          .catch(function (e) { st.jobs = null; st.error = e.message; })
      ]).then(render);
    }
    var j = encodeURIComponent(route.job);
    return Promise.all([local,
      api("GET", "/v1/gateways/" + g + "/jobs/" + j)
        .then(function (d) { st.job = d; })
        .catch(function (e) { st.job = null; st.error = e.message; }),
      api("GET", "/v1/gateways/" + g + "/jobs/" + j + "/events?tail=300")
        .then(function (d) { st.events = d; })
        .catch(function () { st.events = null; })
    ]).then(render);
  }

  function rowFor(name) {
    var rows = (st.local && st.local.gateways) || [];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].gateway.name === name) { return rows[i]; }
    }
    return null;
  }

  // ── render ───────────────────────────────────────────────────────────
  function render() {
    crumbs();
    var view = $("view");
    view.innerHTML = "";
    if (route.level === "gateways") { gatewaysView(view); }
    else if (route.level === "jobs") { jobsView(view); }
    else { eventsView(view); }
  }

  function crumbs() {
    var host = $("crumbs");
    host.innerHTML = "";
    function crumb(label, hash, here) {
      if (here) {
        var span = document.createElement("span");
        span.className = "here";
        span.textContent = label;
        host.appendChild(span);
        return;
      }
      var b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", function () { go(hash); });
      host.appendChild(b);
      var sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "/";
      host.appendChild(sep);
    }
    crumb("gateways", "#/", route.level === "gateways");
    if (route.gateway) {
      crumb(route.gateway, "#/g/" + encodeURIComponent(route.gateway),
            route.level === "jobs");
    }
    if (route.job) { crumb(route.job.slice(0, 8), "", true); }
  }

  // ── level 1: gateways and their tunnels ──────────────────────────────
  function tunnelTone(row) {
    var t = row.tunnel;
    if (!t) { return (row.endpoint || {}).state === "up" ? "success" : ""; }
    if (t.state === "up") { return "success"; }
    if (t.state === "authenticating") { return "warning"; }
    if (t.state === "starting") { return "info"; }
    if (t.state === "retrying" || t.state === "failed") { return "danger"; }
    return "";
  }
  function tunnelWord(row) {
    var t = row.tunnel;
    if (!t) {
      var e = row.endpoint || {};
      return e.state === "up" ? "reachable" : (e.state || "no tunnel");
    }
    if (t.state === "retrying" && t.next_retry_in > 0) {
      return "retry " + Math.ceil(t.next_retry_in) + "s";
    }
    return t.state;
  }

  function gatewaysView(view) {
    var d = st.local;
    if (!d) { return; }
    var head = document.createElement("p");
    head.className = "section";
    head.title = d.config_path;
    // The path is long and the same every time: name the file, keep the
    // directory in the tooltip rather than across the top of every screen.
    head.textContent = d.gateways.length + " gateways · " +
      d.config_path.split("/").pop() +
      (d.writable ? "" : " (read-only: TOML)");
    view.appendChild(head);

    var list = document.createElement("div");
    list.className = "list";
    d.gateways.forEach(function (row) { list.appendChild(gatewayCard(row)); });
    view.appendChild(list);
    view.appendChild(addPanel());
  }

  function gatewayCard(row) {
    var g = row.gateway, t = row.tunnel;
    var card = document.createElement("div");
    card.className = "card";

    var item = document.createElement("div");
    item.className = "item click";
    item.innerHTML =
      '<span class="dot ' + tunnelTone(row) + '"></span>' +
      '<span class="body">' +
        '<span class="head"><span class="title">' + esc(g.name) + '</span>' +
          (row.default ? '<span class="tag">default</span>' : '') +
          '<span class="tag ' + tunnelTone(row) + '">' + esc(tunnelWord(row)) + '</span>' +
          (g.autostart ? '<span class="tag magic">autostart</span>' : '') +
        '</span>' +
        '<span class="sub mono">' + esc(g.base_url) + '</span>' +
        '<span class="meta">' + esc(endpointLine(row)) + '</span>' +
      '</span>';

    // The one-click action, on the row: whichever of start/stop is not the
    // current state. Everything else lives behind "details", so a list of six
    // gateways is six rows rather than six panels.
    var bar = document.createElement("span");
    bar.className = "actions onrow";
    if (g.tunnelled) {
      bar.appendChild(btn(t && t.wanted ? "stop" : "start",
        t && t.wanted ? "" : "primary", function () {
          return api("POST", "/v1/tunnels/" + encodeURIComponent(g.name) +
                     (t && t.wanted ? "/down" : "/up")).then(load);
        }));
    }
    bar.appendChild(btn(open.edit[g.name] ? "hide" : "details", "", function () {
      open.edit[g.name] = !open.edit[g.name];
      render();
      return Promise.resolve();
    }));
    item.appendChild(bar);
    item.appendChild(el("span", "chev", "\u203a"));
    item.addEventListener("click", function (ev) {
      if (ev.target.closest("button")) { return; }
      go("#/g/" + encodeURIComponent(g.name));
    });
    card.appendChild(item);

    // A prompt is never hidden behind a disclosure: ssh is waiting on an
    // answer, and a login that times out because the question was one click
    // away is the failure this whole thing exists to prevent.
    if (t && t.prompt) { card.appendChild(ask(g.name, t)); }

    var trouble = row.problem || g.warning ||
      (t && t.state !== "up" ? t.last_error : "");
    if (trouble && !open.edit[g.name]) {
      card.appendChild(el("div", "detail", '<div class="err">' + esc(trouble) +
                          '</div>', true));
    }
    if (!open.edit[g.name]) { return card; }

    var detail = document.createElement("div");
    detail.className = "detail";
    detail.innerHTML =
      // Not repeated when the console is open: the console's first line is this
      // same command, and printing it twice reads like two of them.
      (g.ssh_display
        ? (open.out[g.name] ? ''
           : '<div class="sub mono">$ ' + esc(g.ssh_display) + '</div>')
        : '<div class="meta">no ssh command — add one and this becomes a tunnel</div>') +
      (row.problem ? '<div class="err">' + esc(row.problem) + '</div>' : '') +
      (g.warning ? '<div class="err">' + esc(g.warning) + '</div>' : '') +
      (t && t.last_error && t.state !== "up"
        ? '<div class="err">' + esc(t.last_error) + '</div>' : '');

    var more = document.createElement("div");
    more.className = "actions";
    more.style.marginTop = "9px";
    if (g.tunnelled) {
      more.appendChild(btn("restart", "", function () {
        return api("POST", "/v1/tunnels/" + encodeURIComponent(g.name) +
                   "/restart").then(load);
      }));
      more.appendChild(btn(open.out[g.name] ? "hide ssh output" : "ssh output",
        "", function () {
          open.out[g.name] = !open.out[g.name];
          render();
          return Promise.resolve();
        }));
    }
    more.appendChild(btn("remove", "danger", function () {
      if (!confirm("Remove " + g.name + " from gateways.json?")) {
        return Promise.resolve();
      }
      return api("DELETE", "/v1/gateways/" + encodeURIComponent(g.name))
        .then(load);
    }));
    detail.appendChild(more);

    if (open.out[g.name]) {
      var pre = document.createElement("pre");
      pre.className = "console";
      pre.id = "out-" + g.name;
      pre.textContent = "…";
      detail.appendChild(pre);
      pullOutput(g.name);
    }
    detail.appendChild(editForm(g));
    card.appendChild(detail);
    return card;
  }

  // Two facts, never merged into one light: ssh can be alive while the port
  // answers nothing, and the fix for each is a different thing to go and do.
  function endpointLine(row) {
    var e = (row.tunnel ? row.tunnel.endpoint : row.endpoint) || {};
    var bits = [];
    if (row.tunnel && row.tunnel.pid) { bits.push("ssh pid " + row.tunnel.pid); }
    if (e.state) { bits.push("endpoint " + e.state); }
    if (e.version) { bits.push("agent-bridge " + e.version); }
    if (e.latency_ms != null) { bits.push(e.latency_ms + " ms"); }
    var g = row.gateway;
    bits.push(g.has_token ? "token from " + g.token_source : "no token");
    if (e.detail) { bits.push(e.detail); }
    return bits.join(" · ");
  }

  function ask(name, t) {
    var box = document.createElement("div");
    box.className = "ask";
    box.style.margin = "0 10px 10px";
    box.innerHTML = '<p>' + esc(t.prompt) + '</p>';
    var line = document.createElement("div");
    line.className = "row";
    var input = document.createElement("input");
    input.type = t.prompt_secret ? "password" : "text";
    input.autocomplete = "off";
    input.placeholder = t.prompt_secret
      ? "sent straight to ssh; not stored" : "answer";
    var send = btn("send", "primary", function () {
      var text = input.value;
      input.value = "";
      return api("POST", "/v1/tunnels/" + encodeURIComponent(name) + "/answer",
                 { text: text }).then(load);
    });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); send.click(); }
    });
    line.appendChild(input);
    line.appendChild(send);
    box.appendChild(line);
    setTimeout(function () { input.focus(); }, 0);
    return box;
  }

  function editForm(g) {
    var form = document.createElement("form");
    form.className = "edit open";
    form.innerHTML =
      '<label>base_url — the local port the CLI connects to</label>' +
      '<input name="base_url" class="mono" value="' + esc(g.base_url) + '">' +
      '<label>ssh command — argv, no shell</label>' +
      '<input name="ssh" class="mono" value="' + esc(g.ssh_display) + '">' +
      '<label><input type="checkbox" name="autostart" style="width:auto"' +
        (g.autostart ? " checked" : "") + '> start with the daemon</label>' +
      '<p class="hint">Saved to gateways.json; the old file is kept as .bak.</p>' +
      '<div class="actions" style="margin-top:6px">' +
        '<button class="btn primary" type="submit">save</button>' +
        '<button class="btn" type="button" data-act="default">make default</button>' +
      '</div><p class="err"></p>';
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      form.querySelector(".err").textContent = "";
      api("PUT", "/v1/gateways/" + encodeURIComponent(g.name), {
        base_url: form.base_url.value.trim(),
        ssh: form.ssh.value.trim(),
        autostart: form.autostart.checked
      }).then(function () {
        open.edit[g.name] = false;
        return load();
      }).catch(function (e) { form.querySelector(".err").textContent = e.message; });
    });
    form.querySelector('[data-act="default"]').addEventListener("click", function () {
      api("POST", "/v1/gateways/" + encodeURIComponent(g.name) + "/default")
        .then(load)
        .catch(function (e) { form.querySelector(".err").textContent = e.message; });
    });
    return form;
  }

  function addPanel() {
    var panel = document.createElement("div");
    panel.className = "panel";
    panel.innerHTML = '<div class="head"><span class="title" style="flex:1">' +
      'Add a gateway</span></div>';
    var toggle = btn(open.add ? "cancel" : "new", "", function () {
      open.add = !open.add;
      render();
      return Promise.resolve();
    });
    panel.querySelector(".head").appendChild(toggle);
    if (!open.add) { return panel; }
    var form = document.createElement("form");
    form.className = "edit open";
    form.innerHTML =
      '<label>name</label><input name="name" placeholder="midway5" required>' +
      '<label>base_url</label><input name="base_url" class="mono" ' +
        'placeholder="http://localhost:8787" required>' +
      '<label>ssh command</label><input name="ssh" class="mono" ' +
        'placeholder="ssh -N -o ServerAliveInterval=60 -L 8787:localhost:8787 midway5">' +
      '<label>token_env</label><input name="token_env" ' +
        'placeholder="AGENT_BRIDGE_TOKEN">' +
      '<div class="actions" style="margin-top:8px">' +
        '<button class="btn primary" type="submit">save</button></div>' +
      '<p class="err"></p>';
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      form.querySelector(".err").textContent = "";
      api("PUT", "/v1/gateways/" +
          encodeURIComponent(form.name.value.trim()), {
        base_url: form.base_url.value.trim(),
        ssh: form.ssh.value.trim(),
        token_env: form.token_env.value.trim() || undefined
      }).then(function () { open.add = false; return load(); })
        .catch(function (e) { form.querySelector(".err").textContent = e.message; });
    });
    panel.appendChild(form);
    return panel;
  }

  function pullOutput(name) {
    api("GET", "/v1/tunnels/" + encodeURIComponent(name) + "/output?after=0")
      .then(function (d) {
        var pre = $("out-" + name);
        if (!pre) { return; }
        pre.innerHTML = d.lines.map(function (l) {
          return '<span class="' + esc(l.kind) + '">' + esc(l.text) + '</span>';
        }).join("\n") || "(nothing yet)";
        pre.scrollTop = pre.scrollHeight;
      }).catch(function () {});
  }

  // ── level 2: jobs ────────────────────────────────────────────────────
  var JOB_TONE = { succeeded: "success", failed: "danger", canceled: "danger",
                   running: "info", waiting: "warning", queued: "" };

  function jobsView(view) {
    var row = rowFor(route.gateway);
    if (row) { view.appendChild(gatewayStrip(row)); }
    if (st.error) {
      var err = document.createElement("div");
      err.className = "panel";
      err.innerHTML = '<div class="err">' + esc(st.error) + '</div>' +
        '<p class="hint">If the tunnel is down, start it from the gateway ' +
        'list — the base_url is a local port that only answers while ssh is up.</p>';
      view.appendChild(err);
      return;
    }
    var jobs = (st.jobs && st.jobs.jobs) || [];
    var head = document.createElement("p");
    head.className = "section";
    head.textContent = "jobs · " + jobs.length +
      (st.jobs && st.jobs.total ? " of " + st.jobs.total : "");
    view.appendChild(head);
    if (!jobs.length) {
      view.appendChild(el("div", "empty", "No jobs on this gateway yet."));
      return;
    }
    var list = document.createElement("div");
    list.className = "list";
    jobs.forEach(function (job) {
      var card = document.createElement("div");
      card.className = "card";
      var item = document.createElement("div");
      item.className = "item click";
      var tone = JOB_TONE[job.status] || "";
      item.innerHTML =
        '<span class="dot ' + tone + '"></span>' +
        '<span class="body">' +
          '<span class="head">' +
            '<span class="title">' + esc(job.title || (job.prompt || "").slice(0, 80) ||
              job.id.slice(0, 8)) + '</span>' +
            '<span class="tag ' + tone + '">' + esc(job.status) + '</span>' +
            (job.agent ? '<span class="tag">' + esc(job.agent) + '</span>' : '') +
          '</span>' +
          '<span class="meta mono">' + esc(job.id) + '</span>' +
          '<span class="meta">' + esc(jobMeta(job)) + '</span>' +
        '</span><span class="chev">›</span>';
      item.addEventListener("click", function () {
        go("#/g/" + encodeURIComponent(route.gateway) + "/j/" +
           encodeURIComponent(job.id));
      });
      card.appendChild(item);
      list.appendChild(card);
    });
    view.appendChild(list);
  }

  function jobMeta(job) {
    var bits = [];
    if (job.created_at) { bits.push("created " + stamp(job.created_at)); }
    if (job.updated_at) { bits.push("updated " + stamp(job.updated_at)); }
    if (job.cost_usd) { bits.push("$" + Number(job.cost_usd).toFixed(4)); }
    if (job.cwd) { bits.push(job.cwd); }
    return bits.join(" · ");
  }

  function gatewayStrip(row) {
    var g = row.gateway;
    var strip = document.createElement("div");
    strip.className = "card";
    strip.style.marginBottom = "10px";
    var inner = document.createElement("div");
    inner.className = "item";
    inner.innerHTML =
      '<span class="dot ' + tunnelTone(row) + '"></span>' +
      '<span class="body"><span class="head">' +
        '<span class="title">' + esc(g.name) + '</span>' +
        '<span class="tag ' + tunnelTone(row) + '">' + esc(tunnelWord(row)) + '</span>' +
      '</span><span class="meta">' + esc(endpointLine(row)) + '</span></span>';
    strip.appendChild(inner);
    if (row.tunnel && row.tunnel.prompt) {
      strip.appendChild(ask(g.name, row.tunnel));
    }
    return strip;
  }

  // ── level 3: one job's events ────────────────────────────────────────
  var EV_TONE = { error: "danger", result: "success", steer: "magic",
                  message: "info", status: "" };

  function eventsView(view) {
    var row = rowFor(route.gateway);
    if (row) { view.appendChild(gatewayStrip(row)); }
    if (st.error || !st.job) {
      view.appendChild(el("div", "panel",
        '<div class="err">' + esc(st.error || "job not found") + '</div>', true));
      return;
    }
    var job = st.job;
    var tone = JOB_TONE[job.status] || "";
    var panel = document.createElement("div");
    panel.className = "panel";
    panel.innerHTML =
      '<div class="head"><span class="title" style="flex:1">' +
        esc(job.title || job.id) + '</span>' +
        '<span class="tag ' + tone + '">' + esc(job.status) + '</span></div>' +
      '<dl class="kv" style="margin-top:8px">' +
        kv("id", '<span class="mono">' + esc(job.id) + '</span>') +
        kv("agent", esc(job.agent || "")) +
        kv("session", '<span class="mono">' + esc(job.session || "—") + '</span>') +
        kv("cwd", '<span class="mono">' + esc(job.cwd || "") + '</span>') +
        kv("created", esc(stamp(job.created_at))) +
        kv("updated", esc(stamp(job.updated_at))) +
        (job.cost_usd ? kv("cost", "$" + Number(job.cost_usd).toFixed(4)) : "") +
        (job.reason ? kv("reason", esc(job.reason)) : "") +
        (job.error ? kv("error", '<span class="err">' + esc(job.error) + '</span>') : "") +
      '</dl>' +
      (job.result ? '<pre class="console">' + esc(job.result) + '</pre>' : '');
    view.appendChild(panel);

    var events = (st.events && st.events.events) || [];
    var head = document.createElement("p");
    head.className = "section";
    head.style.marginTop = "14px";
    head.textContent = "events · " + events.length +
      (st.events && st.events.total ? " of " + st.events.total : "");
    view.appendChild(head);
    if (!events.length) {
      view.appendChild(el("div", "empty", "No events yet."));
      return;
    }
    var list = document.createElement("div");
    list.className = "list";
    events.forEach(function (ev) {
      var line = document.createElement("div");
      line.className = "ev " + ev.type;
      line.innerHTML =
        '<span class="at" title="' + esc(seqTitle(ev)) + '">' +
          esc(elapsedLabel(ev, events)) + '</span>' +
        '<span class="kind"><span class="tag ' + (EV_TONE[ev.type] || "") + '">' +
          esc(ev.type) + '</span></span>' +
        '<span class="text clamp">' + esc(eventText(ev)) + '</span>';
      var text = line.querySelector(".text");
      line.addEventListener("click", function () {
        text.classList.toggle("clamp");
      });
      list.appendChild(line);
    });
    view.appendChild(list);
  }

  // Where in the run this happened, which is the question a reader actually has
  // — a sequence number only says which came first. The gateway already
  // computes both `elapsed` (seconds since the job's first event) and
  // `elapsed_hms`, so prefer its answer; the fallbacks are for a page reading an
  // older gateway, and `#seq` is the honest last resort rather than a made-up
  // "+00:00:00".
  function elapsedLabel(ev, events) {
    if (ev.elapsed_hms) { return ev.elapsed_hms; }
    if (typeof ev.elapsed === "number") { return hms(ev.elapsed); }
    var mine = Date.parse(ev.ts), first = events.length && Date.parse(events[0].ts);
    if (!isNaN(mine) && first && !isNaN(first)) {
      return hms(Math.max(0, (mine - first) / 1000));
    }
    return "#" + ev.seq;
  }

  function hms(seconds) {
    var t = Math.floor(seconds);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return "+" + pad(Math.floor(t / 3600)) + ":" + pad(Math.floor(t % 3600 / 60)) +
      ":" + pad(t % 60);
  }

  function seqTitle(ev) {
    return "seq " + ev.seq + (ev.ts ? " · " + ev.ts : "");
  }

  // One line per event that says the useful thing, with the raw payload one
  // click away. `ab events` prints the same fields; this is the same reading.
  function eventText(ev) {
    var d = ev.data || {};
    switch (ev.type) {
      case "assistant": case "thinking": return d.text || "";
      case "result": return d.text || "";
      case "steer": return d.text || "";
      case "error": return d.message || JSON.stringify(d);
      case "tool_use": return (d.name || "tool") + " " +
        JSON.stringify(d.input || {});
      case "tool_result": return d.text || "";
      case "message": return (d.file ? d.file + ": " : "") + (d.msg || "");
      case "status": return Object.keys(d).map(function (k) {
        return k + "=" + JSON.stringify(d[k]);
      }).join(" ");
      default: return JSON.stringify(d);
    }
  }

  // ── helpers ──────────────────────────────────────────────────────────
  function kv(key, html) {
    return "<dt>" + esc(key) + "</dt><dd>" + html + "</dd>";
  }
  function el(tag, cls, content, isHtml) {
    var node = document.createElement(tag);
    node.className = cls;
    if (isHtml) { node.innerHTML = content; } else { node.textContent = content; }
    return node;
  }
  function btn(label, cls, fn) {
    var b = document.createElement("button");
    b.className = "btn" + (cls ? " " + cls : "");
    b.textContent = label;
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      b.disabled = true;
      fn().catch(function (e) { alert(e.message); })
          .then(function () { b.disabled = false; });
    });
    return b;
  }

  // EventSource cannot send a header, so trade the token for a single-use
  // ticket rather than putting the real one in a url the browser remembers.
  function listen() {
    api("POST", "/v1/events/ticket").then(function (d) {
      var es = new EventSource("/v1/events?after=0&ticket=" +
                               encodeURIComponent(d.ticket));
      es.onopen = function () { mark("live", "success"); };
      es.onmessage = function () { if (route.level === "gateways") { load(); } };
      es.onerror = function () {
        es.close();
        mark("reconnecting", "warning");
        setTimeout(listen, 3000);
      };
    }).catch(function () { setTimeout(listen, 5000); });
  }
  function mark(text, tone) {
    $("link").textContent = text;
    $("link").className = "tag " + tone;
  }

  function tick() {
    // The gateway levels poll; the tunnel level is driven by the event stream
    // and polls slowly behind it. A finished job stops costing anything.
    clearTimeout(timer);
    var every = route.level === "gateways" ? 5000
      : (st.job && ["succeeded", "failed", "canceled"].indexOf(st.job.status) >= 0
         ? 15000 : 2500);
    timer = setTimeout(function () { load().then(tick); }, every);
  }

  function navigate() {
    readRoute();
    st.jobs = st.job = st.events = null;
    render();
    load().then(tick);
  }

  function boot() {
    $("gate").hidden = true;
    $("app").hidden = false;
    readRoute();
    load().then(function () {
      listen();
      tick();
    }).catch(function (e) {
      $("app").hidden = true;
      $("gate").hidden = false;
      $("gate-err").textContent = e.message;
    });
  }

  // The token arrives in the fragment, which browsers never send to the server.
  // Take it once, then hand the fragment over to routing.
  var frag = new URLSearchParams((location.hash || "").replace(/^#/, ""));
  if (frag.get("token")) {
    token = frag.get("token");
    sessionStorage.setItem(KEY, token);
    history.replaceState(null, "", location.pathname + "#/");
  } else {
    token = sessionStorage.getItem(KEY) || "";
  }

  window.addEventListener("hashchange", navigate);
  $("gate-go").addEventListener("click", function () {
    token = $("gate-token").value.trim();
    sessionStorage.setItem(KEY, token);
    boot();
  });
  $("gate-token").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { $("gate-go").click(); }
  });

  if (token) { boot(); } else { $("gate").hidden = false; }
}());
</script>
</html>
"""
