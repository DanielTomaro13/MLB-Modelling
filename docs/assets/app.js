// app.js — MLB 162-0. Multi-page static site over the pipeline's JSON contracts.

/* ---------- helpers ---------- */
const fmtPct = (p) => (p == null ? "—" : (p * 100).toFixed(0) + "%");
const fmtOdds = (p) => (p && p > 0 ? (1 / p).toFixed(2) : "—");
const odds = (v) => (v && v > 0 ? (+v).toFixed(2) : "—");
const getJSON = (p) => fetch(p).then((r) => (r.ok ? r.json() : null)).catch(() => null);

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e[k] = v;
    else e.setAttribute(k, v);
  }
  kids.flat().forEach((c) => e.append(c?.nodeType ? c : document.createTextNode(c ?? "")));
  return e;
}
function bar(p) { const b = el("div", { class: "bar" }); const s = el("span"); s.style.width = ((p || 0) * 100).toFixed(1) + "%"; b.append(s); return b; }

/* ---------- chrome ---------- */
const NAV = [
  ["home", "Home", "index.html"], ["schedule", "Schedule", "schedule.html"],
  ["value", "Value", "value.html"], ["rankings", "Rankings", "rankings.html"],
  ["players", "Players", "players.html"], ["backtest", "Backtest", "backtest.html"],
  ["about", "About", "about.html"],
];
const SISTER_SITES = [
  ["AFL 23-0", "https://afl23-0.com"], ["NRL 24-0", "https://nrl24-0.com"],
  ["NBA 82-0", "https://nba82-0.com"], ["NBL 33-0", "https://nbl33-0.com"],
  ["Tennis Slam", "https://danieltomaro13.github.io/Tennis-Modelling/"],
  ["Football Invincibles", "https://footballinvincibles.com"], ["F1 Slam", "https://f1slam.com"],
  ["MLB 162-0", null],
];
const FOOT_LINKS = [["How it works", "about.html"], ["Backtest", "backtest.html"]];

function sisterBar() {
  return el("div", { class: "sister-bar", role: "navigation", "aria-label": "Sister sites" },
    el("span", { class: "lead" }, "THE 0 SERIES ·"),
    ...SISTER_SITES.map(([label, href]) => href
      ? el("a", { class: "sister-link", href }, label)
      : el("span", { class: "sister-link", "data-active": "true", "aria-current": "page" }, label)));
}

function chrome(page) {
  const strip = sisterBar();
  const header = el("header", {}, el("div", { class: "wrap" },
    el("a", { class: "brand", href: "index.html" },
      el("img", { class: "ball", src: "assets/ball.svg", alt: "", "aria-hidden": "true" }),
      el("span", {}, "MLB "), el("span", { class: "acc" }, "162-0")),
    el("nav", {}, ...NAV.map(([id, label, href]) => el("a", { class: id === page ? "on" : "", href }, label)))));
  const footer = el("footer", {}, el("div", { class: "wrap" },
    el("nav", { class: "foot-links" }, ...FOOT_LINKS.flatMap(([label, href], i) =>
      [i ? el("span", { class: "sep" }, "·") : "", el("a", { href }, label)])),
    el("p", { html: 'Modelled from the public <a href="https://statsapi.mlb.com">MLB Stats API</a>. For research and entertainment only — not betting advice.' }),
    el("p", { class: "series" }, "Part of the 0 Series · ",
      ...SISTER_SITES.filter(([, href]) => href).flatMap(([label, href], i) => [i ? " · " : "", el("a", { href }, label)])),
    el("a", { class: "kofi", href: "https://ko-fi.com/danieltomaro", target: "_blank", rel: "noopener" }, "☕ Support on Ko-fi")));
  document.getElementById("app-header")?.replaceWith(strip, header);
  document.getElementById("app-footer")?.replaceWith(footer);
}

async function freshness() {
  const meta = await getJSON("data/meta.json");
  const f = document.getElementById("freshness");
  if (f && meta) f.textContent = `Season ${meta.season} · ${meta.n_teams} teams · updated ${meta.generated}`;
  return meta;
}

/* ---------- pages ---------- */
async function pageHome(content) {
  const [meta, preds, oddsData] = await Promise.all([
    getJSON("data/meta.json"), getJSON("data/predictions.json"), getJSON("data/odds.json")]);
  const bt = meta?.backtest || {};
  const tiles = el("div", { class: "cards" },
    tile("Today's games", (preds?.count ?? 0) + "", "priced across the full market book"),
    tile("Backtest accuracy", bt.accuracy != null ? fmtPct(bt.accuracy) : "—", `vs ${bt.home_win_rate != null ? fmtPct(bt.home_win_rate) : "—"} home base rate`),
    tile("Log loss", bt.log_loss != null ? bt.log_loss : "—", bt.beats_baseline ? "beats the baseline ✓" : "baseline " + (bt.baseline_log_loss ?? "—")),
    tile("Pitchers / batters", `${meta?.n_pitchers ?? "—"} / ${meta?.n_batters ?? "—"}`, "profiled this season"));
  content.append(tiles);

  // Top value picks
  const picks = (oddsData?.games || []).flatMap((g) => g.selections.map((s) => ({ ...s, g }))).filter((s) => s.ev > 0).sort((a, b) => b.ev - a.ev).slice(0, 8);
  if (picks.length) {
    content.append(el("div", { class: "group-head" }, el("h2", {}, "Top value today"), el("a", { href: "value.html", class: "muted" }, "all value →")));
    content.append(valueTable(picks));
  }
  const nextHref = preds?.count ? "schedule.html" : null;
  content.append(el("a", { class: "banner", href: nextHref || "schedule.html" }, "See every game's model price on the Schedule →"));
}
function tile(h, big, p) { return el("div", { class: "tile" }, el("h3", {}, h), el("div", { class: "big" }, big), el("p", {}, p)); }

async function pageSchedule(content) {
  const preds = await getJSON("data/predictions.json");
  if (!preds || !preds.games?.length) return content.append(el("p", { class: "loading" }, "No upcoming games found."));
  const byDate = {};
  preds.games.forEach((g) => (byDate[g.date] ||= []).push(g));
  for (const date of Object.keys(byDate).sort()) {
    content.append(el("div", { class: "group-head" }, el("h2", {}, date), el("span", { class: "muted" }, byDate[date].length + " games")));
    byDate[date].forEach((g) => content.append(gameCard(g)));
  }
}

function gameCard(g) {
  const ml = g.markets.find((m) => m.key === "ml");
  const tot = g.markets.find((m) => m.key === "total");
  const rl = g.markets.find((m) => m.key === "rl");
  const rows = [
    ["away", g.away, g.awayAbbr, g.awayPitcher, g.win_away, g.fair_away],
    ["home", g.home, g.homeAbbr, g.homePitcher, g.win_home, g.fair_home],
  ].map(([side, name, abbr, pit, wp, fair]) =>
    el("tr", {},
      el("td", { class: "pl" }, el("b", {}, name), el("div", { class: "mut", style: "font-size:12px" }, "P: " + (pit || "TBD"))),
      el("td", {}, bar(wp)),
      el("td", {}, fmtPct(wp)),
      el("td", {}, odds(fair)),
      el("td", {}, rl ? rlCover(rl, side) : "—")));
  const totLine = tot?.lines?.find((l) => l.line === 8.5) || tot?.lines?.[Math.floor((tot?.lines?.length || 1) / 2)];
  return el("div", { class: "match" },
    el("h3", {},
      el("span", {}, `${g.awayAbbr} @ ${g.homeAbbr}`),
      el("span", { class: "ko" }, `proj total ${g.total_mean}`, totLine ? el("span", { class: "park", style: "margin-left:8px" }, `O${totLine.line} ${fmtPct(totLine.over)}`) : "")),
    el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { class: "pl" }, "Team"), el("th", {}, "Win"), el("th", {}, "Prob"), el("th", {}, "Fair"), el("th", {}, "RL ±1.5"))),
      el("tbody", {}, ...rows))));
}
function rlCover(rl, side) {
  const sel = rl.selections.find((s) => s.label.startsWith(side));
  return sel ? `${fmtPct(sel.prob)} · ${odds(sel.fair)}` : "—";
}

async function pageValue(content) {
  const data = await getJSON("data/odds.json");
  if (!data || !data.games?.length)
    return content.append(el("p", { class: "loading" }, "No bookmaker odds loaded yet. Odds refresh from a local run; check back soon."));
  content.append(el("p", { class: "mut" }, `Books: ${(data.books || []).join(", ") || "—"} · ${data.count} games · updated ${data.generated}`));
  const all = data.games.flatMap((g) => g.selections.map((s) => ({ ...s, g }))).sort((a, b) => b.ev - a.ev);
  const pos = all.filter((s) => s.ev > 0);
  content.append(el("div", { class: "group-head" }, el("h2", {}, "Positive expected value"), el("span", { class: "muted" }, pos.length + " selections")));
  content.append(valueTable(pos.length ? pos : all.slice(0, 25)));
}

function valueTable(rows) {
  return el("div", { class: "match" }, el("div", { class: "tablewrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { class: "pl" }, "Game"), el("th", { class: "pl" }, "Selection"),
      el("th", {}, "Model"), el("th", {}, "Fair"), el("th", {}, "Best"), el("th", {}, "Book"), el("th", {}, "EV"))),
    el("tbody", {}, ...rows.map((s) =>
      el("tr", {},
        el("td", { class: "pl mut" }, `${s.g.awayAbbr} @ ${s.g.homeAbbr}`),
        el("td", { class: "pl" }, s.label),
        el("td", {}, fmtPct(s.model)),
        el("td", {}, odds(s.fair)),
        el("td", { class: "bestbook" }, odds(s.best?.price)),
        el("td", { class: "mut" }, s.best?.book || "—"),
        el("td", { class: s.ev > 0 ? "pos" : "neg" }, (s.ev > 0 ? "+" : "") + (s.ev * 100).toFixed(1) + "%")))))));
}

async function pageRankings(content) {
  const data = await getJSON("data/ratings.json");
  if (!data || !data.teams?.length) return content.append(el("p", { class: "loading" }, "Ratings not available."));
  content.append(el("div", { class: "match rank" }, el("div", { class: "tablewrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", { class: "pl" }, "Team"), el("th", { class: "pl" }, "Division"), el("th", {}, "Elo"), el("th", {}, "Games"))),
    el("tbody", {}, ...data.teams.map((t) =>
      el("tr", {},
        el("td", { class: "mut" }, t.rank),
        el("td", { class: "pl" }, el("b", {}, t.name), " ", el("span", { class: "pill" }, t.abbr)),
        el("td", { class: "pl div" }, t.division),
        el("td", { class: "elo" }, t.elo),
        el("td", { class: "mut" }, t.played))))))));
}

async function pagePlayers(content) {
  const [index, players] = await Promise.all([getJSON("data/players-index.json"), getJSON("data/players.json")]);
  if (!index) return content.append(el("p", { class: "loading" }, "Players not available."));
  const input = el("input", { class: "search", type: "search", placeholder: "Search players…", autocomplete: "off" });
  const role = el("select", {}, el("option", { value: "" }, "All"), el("option", { value: "B" }, "Batters"), el("option", { value: "P" }, "Pitchers"));
  const count = el("span", { class: "count" });
  content.append(el("div", { class: "filters" }, input, role, count));
  const host = el("div", { class: "match" }); content.append(host);

  function render() {
    const q = input.value.trim().toLowerCase(), r = role.value;
    const rows = index.filter((p) => (!r || p.role === r) && p.name.toLowerCase().includes(q)).slice(0, 120);
    count.textContent = rows.length + " shown";
    host.replaceChildren(el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { class: "pl" }, "Player"), el("th", {}, "Pos"), el("th", {}, "Team"), el("th", { class: "pl" }, "Key stats"))),
      el("tbody", {}, ...rows.map((p) => {
        const pl = players?.[p.id];
        const stats = pl ? Object.entries(pl.stats).filter(([, v]) => v !== false).slice(0, 4).map(([k, v]) => `${k} ${v}`).join(" · ") : "";
        return el("tr", {}, el("td", { class: "pl" }, el("b", {}, p.name)), el("td", {}, p.role), el("td", { class: "mut" }, p.team), el("td", { class: "pl mut" }, stats));
      })))));
  }
  input.oninput = render; role.onchange = render; render();
}

async function pageBacktest(content) {
  const meta = await getJSON("data/meta.json");
  const bt = meta?.backtest;
  if (!bt || !bt.n) return content.append(el("p", { class: "loading" }, "Backtest not available."));
  content.append(el("div", { class: "cards" },
    tile("Games scored", bt.n.toLocaleString(), `holdout from ${bt.holdout_start_season}`),
    tile("Accuracy", fmtPct(bt.accuracy), `home base rate ${fmtPct(bt.home_win_rate)}`),
    tile("Log loss", bt.log_loss, `baseline ${bt.baseline_log_loss}`),
    tile("Brier", bt.brier, bt.beats_baseline ? "beats baseline ✓" : "below baseline")));
  content.append(el("p", { class: "mut" }, "Walk-forward: each game is predicted from the team-Elo ratings as they stood before first pitch, then scored against the result. Elo is updated chronologically, so no future information leaks into a prediction."));
}

async function pageAbout(content) {
  const meta = await getJSON("data/meta.json");
  const steps = [
    ["Ingest", "Cache teams, standings, and season hitting/pitching lines from the public MLB Stats API; derive every final score for the Elo + backtest."],
    ["Profiles", "Park- and sample-adjusted team offense/defense ratings, plus per-pitcher run-prevention and per-batter rate profiles (shrunk toward league for small samples)."],
    ["Ratings", "Chronological team Elo — home-field adjusted, margin-of-victory weighted, regressed between seasons."],
    ["Engine", "Each team's runs are a negative-binomial off its offense vs the opposing starter, bullpen and ballpark; the two marginals form a joint score grid that every market is read from."],
    ["Predict", "The sim moneyline is blended with Elo (in logit space) for the headline, then the full book is priced: moneyline, run line, totals, first-5, team totals, correct score, player props."],
    ["Odds", "Each bookmaker board is mapped to the model markets and compared — best price, fair price, expected value."],
  ];
  content.append(el("p", { class: "mut" }, "A reproducible, dependency-free Python pipeline rebuilt every few hours. Everything below comes from public data."));
  content.append(el("div", { class: "cards" }, ...steps.map(([h, p]) => el("div", { class: "tile" }, el("h3", {}, h), el("p", {}, p)))));
  if (meta?.backtest?.n)
    content.append(el("a", { class: "banner", href: "backtest.html" }, `Backtested on ${meta.backtest.n.toLocaleString()} games — ${fmtPct(meta.backtest.accuracy)} accuracy, log loss ${meta.backtest.log_loss}. See the backtest →`));
}

/* ---------- boot ---------- */
const PAGES = { home: pageHome, schedule: pageSchedule, value: pageValue, rankings: pageRankings, players: pagePlayers, backtest: pageBacktest, about: pageAbout };
(async function () {
  const page = document.body.dataset.page || "home";
  chrome(page);
  await freshness();
  const content = document.getElementById("content");
  if (content && PAGES[page]) {
    content.replaceChildren();
    try { await PAGES[page](content); }
    catch (e) { content.append(el("p", { class: "loading" }, "Something went wrong loading this page.")); console.error(e); }
  }
})();
