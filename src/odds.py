"""Odds — overlay bookmaker prices on the model book and surface value.

For every upcoming game we rebuild the exact run-scoring distribution (so any posted
line is priced precisely), pull each book's MLB board, map the book's market/selection
naming to a canonical model market (moneyline, run line, total runs, first-5 total,
team totals), and compute fair price / best price / EV / edge.

Books — each wrapped so one failing never breaks the rest:
  * Sportsbet  (public apigw JSON, no auth)            — class 18
  * Ladbrokes  (Entain REST, curl_cffi)                — baseball category UUID
  * PointsBet  (curl_cffi)                             — MLB competition
  * TAB        (curl_cffi, OAuth env creds)            — Baseball competitions
  * Dabble     (curl_cffi, env auth) + Pick'em feed    — baseball sportId

Australian books geo-restrict to AU IPs, so this stage is best-effort and is meant to
run from a local AU cron (scripts/odds-cron.sh). Output: docs/data/odds.json (plus
docs/data/pickem-lines.json for the Dabble Pick'em page).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse

from . import fixtures, ratings, sim, util

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
NUM = re.compile(r"([+-]?\d+(?:\.\d+)?)")
_PICKEM: list[dict] = []


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _get(url, retries=2, timeout=30):
    return util.http_get_json(url, retries=retries, timeout=timeout)


def _cffi():
    try:
        from curl_cffi import requests as creq
        return creq
    except Exception:  # noqa: BLE001
        return None


def _urllib_json(url, headers, timeout=25):
    """Plain-stdlib GET with custom headers — the fallback when curl_cffi is absent.
    Works for the books that don't need browser impersonation (PointsBet, Ladbrokes)."""
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _cget(url, headers, impersonate="chrome", timeout=25):
    """Prefer curl_cffi (handles Cloudflare/JA3 for TAB/Dabble); fall back to urllib."""
    creq = _cffi()
    if creq is not None:
        try:
            r = creq.get(url, headers=headers, impersonate=impersonate, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
    return _urllib_json(url, headers, timeout)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def _signed(*vals) -> float | None:
    for v in vals:
        if v in (None, ""):
            continue
        m = NUM.search(str(v))
        if m:
            return float(m.group(1))
    return None


# --------------------------------------------------------------------------- #
# Sportsbet — baseball = class 18
# --------------------------------------------------------------------------- #
SB = "https://www.sportsbet.com.au/apigw/sportsbook-sports/Sportsbook/Sports"
SB_BASEBALL = 18
MLB_HINTS = ("mlb", "major league")


def sb_events():
    comps = _get(f"{SB}/{SB_BASEBALL}/Competitions") or []
    out = []
    for c in comps:
        if not any(h in (c.get("name") or "").lower() for h in MLB_HINTS):
            continue
        d = _get(f"{SB}/Competitions/{c.get('id')}?displayType=default&eventFilter=matches")
        for e in (d or {}).get("events", []):
            if e.get("participant1") and e.get("participant2"):
                out.append({"id": e["id"], "away": e["participant1"], "home": e["participant2"]})
    return out


def sb_markets(ev):
    d = _get(f"{SB}/Events/{ev['id']}/Markets")
    out = []
    for m in (d if isinstance(d, list) else []):
        sels = [{"name": s.get("name", ""), "price": (s.get("price") or {}).get("winPrice"),
                 "hcap": s.get("unformattedHandicap"), "disp": s.get("displayHandicap")}
                for s in m.get("selections", [])]
        out.append((m.get("name", ""), sels))
    return out


# --------------------------------------------------------------------------- #
# Ladbrokes — Entain REST, baseball category UUID
# --------------------------------------------------------------------------- #
LAD = "https://api.ladbrokes.com.au"
LAD_HDR = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.ladbrokes.com.au",
           "Referer": "https://www.ladbrokes.com.au/", "Content-Type": "application/json"}
LAD_BASEBALL = "02721435-4671-4cd0-98f7-15d41ee4103e"


def lad_events():
    q = urllib.parse.quote(json.dumps([LAD_BASEBALL]))
    d = _cget(f"{LAD}/v2/sport/event-request?category_ids={q}", LAD_HDR)
    out = []
    for eid, e in ((d or {}).get("events", {}) or {}).items():
        nm = e.get("name", "")
        for sep in (" v ", " vs ", " @ "):
            if sep in nm:
                a, b = nm.split(sep, 1)
                # Entain names are "Away v Home" (or "Away @ Home").
                out.append({"id": eid, "away": a.strip(), "home": b.strip()})
                break
    return out


def lad_markets(ev):
    d = _cget(f"{LAD}/v2/sport/event-card?id={ev['id']}", LAD_HDR)
    if not d:
        return []
    prices = d.get("prices", {})

    def price(ent_id):
        for kk, v in prices.items():
            if kk.startswith(ent_id + ":"):
                o = (v or {}).get("odds") or {}
                if "decimal" in o:
                    return round(float(o["decimal"]), 2)
                try:
                    return round(1 + float(o["numerator"]) / float(o["denominator"]), 2)
                except Exception:  # noqa: BLE001
                    return None
        return None

    by_market = {}
    for ent in d.get("entrants", {}).values():
        by_market.setdefault(ent.get("market_id"), []).append(ent)

    out = []
    for mid, ents in by_market.items():
        m = d.get("markets", {}).get(mid) or {}
        mname = m.get("name", "")
        mh = m.get("handicap")  # Ladbrokes carries the line/handicap on the market, not the entrant
        sels = []
        for e in ents:
            nm = e.get("name", "")
            hc = None
            if mname == "Line" and mh is not None:
                # Entain convention: home entrant's signed handicap is -market.handicap,
                # away entrant's is +market.handicap (the two Line markets cover ±1.5).
                hc = -mh if e.get("home_away") == "HOME" else mh
            elif mname == "Total" and mh is not None:
                hc = abs(mh)
            sels.append({"name": nm, "price": price(e["id"]), "hcap": hc, "disp": nm})
        out.append((mname, sels))
    return out


# --------------------------------------------------------------------------- #
# PointsBet — MLB competition 112658
# --------------------------------------------------------------------------- #
PB = "https://api.au.pointsbet.com/api/mes/v3"
PB_V2 = "https://api.au.pointsbet.com/api/v2"
PB_HDR = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Origin": "https://pointsbet.com.au"}


def pb_events():
    d = _cget(f"{PB_V2}/sports/list/", PB_HDR)
    if not d:
        return []
    sports = d.get("sports", d) if isinstance(d, dict) else d
    keys = [c.get("key") for s in sports if str(s.get("name", "")).strip().lower() == "baseball"
            for c in s.get("competitions", []) if str(c.get("name", "")).strip().lower() == "mlb"]
    out = []
    for key in keys:
        feat = _cget(f"{PB}/events/featured/competition/{key}", PB_HDR)
        for ev in (feat.get("events", []) if isinstance(feat, dict) else feat) or []:
            if ev.get("homeTeam") and ev.get("awayTeam"):
                out.append({"id": ev.get("key") or ev.get("eventId") or ev.get("id"),
                            "home": ev["homeTeam"], "away": ev["awayTeam"]})
    return out


def pb_markets(ev):
    det = _cget(f"{PB}/events/{ev['id']}", PB_HDR)
    if not det:
        return []
    out = []
    for m in (det.get("fixedOddsMarkets") or det.get("markets") or []):
        sels = [{"name": o.get("name", ""), "price": o.get("price"), "hcap": o.get("points"), "disp": o.get("name", "")}
                for o in (m.get("outcomes") or [])]
        out.append((m.get("name", ""), sels))
    return out


# --------------------------------------------------------------------------- #
# TAB — OAuth env creds, Baseball competitions
# --------------------------------------------------------------------------- #
TAB = "https://api.beta.tab.com.au/v1/tab-info-service"


def _tab_token():
    cid, csec = os.environ.get("TAB_CLIENT_ID", "").strip(), os.environ.get("TAB_CLIENT_SECRET", "").strip()
    if cid and csec:
        body = {"grant_type": "client_credentials", "client_id": cid, "client_secret": csec}
        creq = _cffi()
        if creq is not None:
            try:
                r = creq.post("https://api.beta.tab.com.au/oauth/token", data=body,
                              headers={"Accept": "application/json"}, impersonate="chrome", timeout=15)
                if r.status_code == 200 and r.json().get("access_token"):
                    return r.json()["access_token"]
            except Exception:  # noqa: BLE001
                pass
        # urllib fallback — the OAuth endpoint is a plain form POST, no impersonation needed.
        try:
            import urllib.request
            data = urllib.parse.urlencode(body).encode()
            req = urllib.request.Request("https://api.beta.tab.com.au/oauth/token", data=data,
                                         headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                tok = json.loads(resp.read().decode("utf-8", "replace")).get("access_token")
                if tok:
                    return tok
        except Exception:  # noqa: BLE001
            pass
    return os.environ.get("TAB_ACCESS_TOKEN", "").strip() or None


def tab_events():
    tok = _tab_token()
    if not tok:
        return []
    hdr = {"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    lst = _cget(f"{TAB}/sports/Baseball/competitions?jurisdiction=NSW&homeState=NSW", hdr)
    out = []
    for comp in (lst or {}).get("competitions", []):
        if "mlb" not in (comp.get("name", "") or "").lower() and "major league" not in (comp.get("name", "") or "").lower():
            continue
        for t in comp.get("tournaments", []):
            link = (t.get("_links") or {}).get("self") or (t.get("_links") or {}).get("matches")
            if not link:
                continue
            d = _cget(link, hdr)
            for m in (d or {}).get("matches", []):
                cons = m.get("contestants") or []
                if len(cons) != 2:
                    continue
                raw = [(mk.get("betOption", ""),
                        [{"name": pp.get("name", ""), "price": pp.get("returnWin"), "hcap": pp.get("handicap"), "disp": pp.get("name", "")}
                         for pp in mk.get("propositions", [])])
                       for mk in m.get("markets", [])]
                # TAB lists home first or away first inconsistently; the matcher handles either.
                out.append({"id": m.get("id"), "home": cons[0].get("name"), "away": cons[1].get("name"), "raw": raw})
    return out


# --------------------------------------------------------------------------- #
# Dabble — baseball sportId, curl_cffi + env auth; Pick'em handled separately
# --------------------------------------------------------------------------- #
DAB = "https://api.dabble.com.au"
DAB_BASEBALL = "656e9e24-f955-4413-8bcb-4d7026b1f9bb"
DAB_PICKEM_STAT = {  # Dabble stat slug -> model prop stat label
    "hits": "Hits", "total-bases": "Total bases", "home-runs": "Home run",
    "stolen-bases": "Stolen base", "strikeouts": "Strikeouts", "pitcher-strikeouts": "Strikeouts",
    "rbis": "RBIs", "runs": "Runs", "walks": "Walks", "doubles": "Doubles",
    "pitching-outs": "Outs", "outs": "Outs", "earned-runs": "Earned runs", "hits-allowed": "Hits allowed",
}


def _dab_headers():
    h = {"accept": "application/json",
         "x-device-id": os.environ.get("DABBLE_DEVICE_ID", "00000000-0000-0000-0000-000000000000"),
         "user-agent": os.environ.get("DABBLE_UA", "Dabble/1000041710 CFNetwork/3826.600.41.2.1 Darwin/24.6.0"),
         "x-app-version": os.environ.get("DABBLE_APP_VERSION", "4.17.10+019ededb"), "accept-language": "en-AU,en;q=0.9"}
    auth = os.environ.get("DABBLE_AUTH", "").strip()
    if auth:
        h["authorization"] = auth if auth.lower().startswith("bearer ") else "Bearer " + auth
    if os.environ.get("DABBLE_COOKIE", "").strip():
        h["cookie"] = os.environ["DABBLE_COOKIE"].strip()
    return h


def _dab_get(path):
    creq = _cffi()
    if creq is not None:
        try:
            r = creq.get(DAB + path, headers=_dab_headers(), impersonate="safari_ios", timeout=25)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
    return _urllib_json(DAB + path, _dab_headers(), timeout=25)


def _dab_comps():
    d = _dab_get(f"/competitions/active?sportId={DAB_BASEBALL}")
    data = d.get("data", d) if isinstance(d, dict) else d
    if isinstance(data, dict):
        return data.get("competitions") or next((v for v in data.values() if isinstance(v, list)), [])
    return data or []


def _dab_fixtures(comp_id):
    fx = _dab_get(f"/frontend-api/competitions/{comp_id}/sport-fixtures?includeInPlay=false&exclude%5B%5D=none")
    return (fx.get("data", fx) if isinstance(fx, dict) else fx) or []


def dab_events():
    out = []
    for comp in _dab_comps():
        if "pick" in (comp.get("name") or "").lower():
            continue
        for f in _dab_fixtures(comp["id"]):
            name = f.get("name", "")
            for sep in (" v ", " @ ", " vs "):
                if f.get("id") and sep in name:
                    a, b = [x.strip() for x in name.split(sep, 1)]
                    out.append({"id": f["id"], "away": a, "home": b})
                    break
    return out


def dab_markets(ev):
    detail = _dab_get(f"/frontend-api/sport-fixtures/details/{ev['id']}")
    sfd = (detail or {}).get("sportFixtureDetail") or (detail or {}).get("data", {}).get("sportFixtureDetail") or {}
    if not sfd:
        return []
    sel_name = {s["id"]: s.get("name", "") for s in sfd.get("selections", [])}
    by_mkt = {}
    for p in sfd.get("prices", []):
        by_mkt.setdefault(p.get("marketId"), []).append({"name": sel_name.get(p.get("selectionId")), "price": p.get("price"), "hcap": None, "disp": sel_name.get(p.get("selectionId"))})
    out = []
    for m in sfd.get("markets", []):
        rt = (m.get("resultingType") or "").lower()
        if "pickem" in rt or (m.get("isSgmAllowed") and not m.get("isSingleAllowed")):
            continue
        out.append((m.get("name", ""), by_mkt.get(m.get("id"), [])))
    return out


# Dabble's Pick'em product is branded "Swish": the Pick'em player lines are the fixture
# markets whose resultingType is one of these (NOT swish_team_*/match_*/to_score_*/first_*).
SWISH_STAT = {
    "swish_hits": "Hits", "swish_runs": "Runs", "swish_rbis": "RBIs",
    "swish_total_bases": "Total bases", "swish_home_runs": "Home run", "swish_doubles": "Doubles",
    "swish_batter_walks": "Walks", "swish_stolen_bases": "Stolen base",
    "swish_hits_runs_rbis": "Hits+Runs+RBIs", "swish_strikeouts": "Strikeouts",
    "swish_hits_allowed": "Hits allowed", "swish_earned_runs": "Earned runs",
    "swish_outs": "Outs", "swish_walks": "Walks",
}


def dab_pickem():
    """Scrape Dabble's actual Pick'em ('Swish') player lines from each MLB fixture."""
    for comp in _dab_comps():
        nm = (comp.get("name") or "").lower()
        if "mlb" not in nm and "major league" not in nm:
            continue
        for f in _dab_fixtures(comp["id"]):
            event = f.get("name", "")
            detail = _dab_get(f"/frontend-api/sport-fixtures/details/{f['id']}")
            sfd = (detail or {}).get("sportFixtureDetail") or (detail or {}).get("data", {}).get("sportFixtureDetail") or {}
            sel_name = {s["id"]: s.get("name", "") for s in sfd.get("selections", [])}
            prices = {}
            for p in sfd.get("prices", []):
                prices.setdefault(p.get("marketId"), {})[sel_name.get(p.get("selectionId"))] = p.get("price")
            for m in sfd.get("markets", []):
                stat = SWISH_STAT.get(m.get("resultingType", ""))
                mn = m.get("name", "")
                if not stat or " - " not in mn:
                    continue
                player = mn.split(" - ")[0].strip()
                line = _signed(mn.rsplit("(", 1)[-1] if "(" in mn else None, mn)
                if line is None:
                    continue
                pr = prices.get(m.get("id"), {})
                over = next((v for k, v in pr.items() if "over" in (k or "").lower()), None)
                under = next((v for k, v in pr.items() if "under" in (k or "").lower()), None)
                row = {"event": event, "player": player, "stat": stat, "line": line,
                       "over": over, "under": under}
                if row not in _PICKEM:
                    _PICKEM.append(row)


BOOKS = {
    "sportsbet": (sb_events, sb_markets),
    "ladbrokes": (lad_events, lad_markets),
    "pointsbet": (pb_events, pb_markets),
    "tab": (tab_events, lambda ev: ev.get("raw", [])),
    "dabble": (dab_events, dab_markets),
}
BOOK_LABEL = {"sportsbet": "Sportsbet", "ladbrokes": "Ladbrokes", "pointsbet": "PointsBet", "tab": "TAB", "dabble": "Dabble"}


# --------------------------------------------------------------------------- #
# Canonical market mapping  (book market + selections -> model selections)
# --------------------------------------------------------------------------- #
def _team_side(name, home, away):
    n = norm(name)
    nh, na = norm(home), norm(away)
    if n == nh or (len(nh) >= 4 and nh in n):
        return "home"
    if n == na or (len(na) >= 4 and na in n):
        return "away"
    # surname/nickname fallback: last word
    last = norm((name or "").split()[-1]) if name else ""
    if last and len(last) >= 4:
        if last in nh:
            return "home"
        if last in na:
            return "away"
    return None


# Exact main-market names we price, per canonical type. Anything else (3-way,
# odd/even, winning margin, race-to, alternates, innings props) is ignored — far
# safer than keyword-guessing across a book's dozens of exotic markets.
ML_NAMES = {"money line", "moneyline", "head to head", "match betting", "h2h", "match result", "winner", "match winner"}
RL_NAMES = {"run line", "line", "handicap", "point spread", "spread", "run line (2-way)"}
TOTAL_NAMES = {"total runs", "total match runs", "total", "totals", "match total", "total runs over/under"}
F5_TOTAL_NAMES = {"1st 5 innings total runs", "first 5 innings total runs", "1st half total runs",
                  "first half total runs", "1st half total", "1st 5 innings total"}
F5_ML_NAMES = {"1st half money line", "1st 5 innings money line", "first 5 innings money line",
               "1st half result", "1st 5 innings result"}
FI_NAMES = {"1st inning total runs", "first inning total runs", "1st inning runs", "run in 1st inning"}


def canon(mname, sels, home, away):
    """Yield (mkt, side, line, price, label) for each priced selection in a main market."""
    # PointsBet appends " (Away @ Home)" to every market name — strip it before matching
    # (also so the team-names in that suffix don't masquerade as a team-total market).
    clean = re.sub(r"\s*\([^)]*\)\s*$", "", (mname or "").strip()).strip()
    low = clean.lower()
    out = []

    def emit(mkt, side, line, price, label):
        # A real pre-game MLB price sits in ~[1.1, 26]; outside that the market is
        # suspended / in-play / a placeholder, so it never reaches the value board.
        if price and 1.1 <= float(price) <= 26.0:
            out.append((mkt, side, line, float(price), label))

    def over_under(s):
        sn = (s.get("name") or "").lower()
        return "over" if "over" in sn else "under" if "under" in sn else None

    # Moneyline — strict 2-way; drop tie/draw; reject lopsided placeholder lines.
    if low in ML_NAMES:
        live = [s for s in sels if s.get("price") and "tie" not in (s["name"] or "").lower() and "draw" not in (s["name"] or "").lower()]
        prices = [float(s["price"]) for s in live if s.get("price")]
        # No real MLB moneyline is shorter than ~$1.25 or longer than ~$6 at the main line.
        if len(live) == 2 and prices and min(prices) >= 1.25 and max(prices) <= 6.5:
            for s in live:
                side = _team_side(s["name"], home, away)
                if side:
                    emit("ml", side, None, s["price"], home if side == "home" else away)
        return out

    # Run line — standard ±1.5 plus the ±2.5/±3.5 alternates books bundle here.
    if low in RL_NAMES:
        for s in sels:
            side = _team_side(s["name"], home, away)
            h = _signed(s.get("disp"), s.get("hcap"), s["name"])
            if side and h is not None and 1.0 <= abs(h) <= 3.5:
                emit("rl", side, h, s["price"], f"{home if side == 'home' else away} {h:+g}")
        return out

    # First-5 moneyline — 3-way (home / tie / away).
    if low in F5_ML_NAMES:
        for s in sels:
            sn = (s["name"] or "").lower()
            if "tie" in sn or "draw" in sn:
                emit("f5_ml", "tie", None, s["price"], "First 5 — tie")
            else:
                side = _team_side(s["name"], home, away)
                if side:
                    emit("f5_ml", side, None, s["price"], f"First 5 — {home if side == 'home' else away}")
        return out

    # First-inning runs (NRFI / YRFI) — books post it as a total over/under 0.5.
    if low in FI_NAMES:
        for s in sels:
            ou, line = over_under(s), _signed(s.get("hcap"), s["name"])
            if ou and line is not None and abs(line - 0.5) < 0.01:
                emit("fi", ou, 0.5, s["price"], "Run in 1st inning (YRFI)" if ou == "over" else "No run 1st inning (NRFI)")
        return out

    # First-5 total — typical F5 lines cluster around 4–5 runs.
    if low in F5_TOTAL_NAMES:
        for s in sels:
            ou, line = over_under(s), _signed(s.get("hcap"), s["name"])
            if ou and line is not None and 3.0 <= line <= 7.5:
                emit("f5_total", ou, line, s["price"], f"F5 {ou.title()} {line:g}")
        return out

    # Team totals — "{Team} Total Runs" (Sportsbet/PB) or "{Team} Runs U/O x.x" (Dabble).
    # Require "run" so team prop markets (Total Bases, Hits, Home Runs) don't match.
    team_in_market = _team_side(clean, home, away)
    if team_in_market and "run" in low and ("total" in low or "u/o" in low or "o/u" in low):
        for s in sels:
            ou, line = over_under(s), _signed(s.get("hcap"), s["name"])
            if ou and line is not None and 3.5 <= line <= 6.5:  # typical team-total band
                emit("team_total", f"{team_in_market}_{ou}", line, s["price"],
                     f"{home if team_in_market == 'home' else away} {ou.title()} {line:g}")
        return out

    # Full-game total runs — standard MLB totals sit ~6.5–12.5.
    if low in TOTAL_NAMES:
        for s in sels:
            ou, line = over_under(s), _signed(s.get("hcap"), s["name"])
            if ou and line is not None and 6.0 <= line <= 13.5:
                emit("total", ou, line, s["price"], f"{ou.title()} {line:g}")
        return out

    return out


def model_prob(dist, mkt, side, line):
    if mkt == "ml":
        return dist["win_home"] if side == "home" else dist["win_away"]
    if mkt == "rl":
        return dist["handicap_cover"](side, line)
    if mkt == "total":
        ov = dist["total_over"](line)
        return ov if side == "over" else 1 - ov
    if mkt == "f5_total":
        ov = dist["total_over"](line, f5=True)
        return ov if side == "over" else 1 - ov
    if mkt == "team_total":
        tside, ou = side.split("_")
        ov = dist["team_total_over"](tside, line)
        return ov if ou == "over" else 1 - ov
    if mkt == "f5_ml":
        return {"home": dist["f5_win_home"], "away": dist["f5_win_away"], "tie": dist["f5_tie"]}.get(side)
    if mkt == "fi":
        return (1 - dist["nrfi"]) if side == "over" else dist["nrfi"]
    return None


MARKET_LABEL = {"ml": "Moneyline", "rl": "Run line", "total": "Total runs", "f5_total": "First 5 — total",
                "f5_ml": "First 5 — winner", "fi": "First inning (NRFI/YRFI)", "team_total": "Team totals",
                "prop": "Player props"}
MARKET_ORDER = ["ml", "rl", "total", "f5_total", "f5_ml", "fi", "team_total", "prop"]

# Bookmaker player-prop stat phrase -> model prop stat label. Books prefix with
# "batter"/"pitcher", so cover both forms.
PROP_STAT = {
    "total bases": "Total bases", "batter total bases": "Total bases",
    "hits": "Hits", "batter hits": "Hits",
    "home runs": "Home run", "home run": "Home run", "batter home runs": "Home run",
    "rbis": "RBIs", "rbi": "RBIs", "batter rbis": "RBIs",
    "runs": "Runs", "runs scored": "Runs", "batter runs": "Runs", "batter runs scored": "Runs",
    "walks": "Walks", "batter walks": "Walks",
    "strikeouts": "Strikeouts", "batter strikeouts": "Strikeouts", "batter strike outs": "Strikeouts",
    "stolen bases": "Stolen base", "stolen base": "Stolen base", "batter stolen bases": "Stolen base",
    "doubles": "Doubles", "batter doubles": "Doubles",
    "hits, runs & rbis": "Hits+Runs+RBIs", "hits runs & rbis": "Hits+Runs+RBIs",
    "hits, runs and rbis": "Hits+Runs+RBIs", "hits + runs + rbis": "Hits+Runs+RBIs",
    # Pitcher props (model: Strikeouts, Walks, Outs, Earned runs, Hits allowed).
    "pitcher strikeouts": "Strikeouts", "pitcher walks": "Walks", "pitcher outs": "Outs",
    "pitcher earned runs": "Earned runs", "pitcher hits allowed": "Hits allowed",
}


def parse_player_prop(mname, sels):
    """Individual player props, e.g. Dabble '{Player} - {Stat} O/U ({line})'.
    Yields (player, model_stat, over/under, line, price)."""
    if " - " not in mname or "o/u" not in mname.lower():
        return []
    player, _, rest = mname.partition(" - ")
    stat = PROP_STAT.get(rest.lower().split(" o/u")[0].strip())
    if not stat:
        return []
    out = []
    for s in sels:
        sn = (s.get("name") or "").lower()
        ou = "over" if "over" in sn else "under" if "under" in sn else None
        line = _signed(s.get("hcap"), s.get("name"), mname)
        if ou and line is not None and s.get("price"):
            out.append((player.strip(), stat, ou, line, float(s["price"])))
    return out


# --------------------------------------------------------------------------- #
def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:  # noqa: BLE001
        util.log(f"  [odds] {getattr(fn, '__name__', fn)} failed: {exc}")
        return None


def _match_event(events, home, away):
    nh, na = norm(home), norm(away)
    for e in events:
        eh, ea = norm(e.get("home", "")), norm(e.get("away", ""))
        if (nh in eh or eh in nh) and (na in ea or ea in na):
            return e
        if (nh in ea or ea in nh) and (na in eh or eh in na):  # books that flip home/away
            return e
    return None


def _now():
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def run(cfg):
    dd = cfg["paths"]["docs_data_dir"]
    md = cfg["paths"]["models_dir"]
    profiles = util.read_json(util.abspath(os.path.join(md, "profiles.json")))
    lg = profiles.get("league", {}).get("runs_per_team_game")
    if lg:
        cfg["sim"]["league_runs_per_team"] = lg
    elo_path = util.abspath(os.path.join(md, "elo.json"))
    elo = util.read_json(elo_path) if os.path.exists(elo_path) else None
    fxs = fixtures.load_fixtures(cfg, profiles)

    # Model player-prop projections (mu per player+stat), indexed by matchup, for pricing
    # the books' individual player-prop markets.
    pred_path = util.abspath(os.path.join(dd, "predictions.json"))
    pred_index = {}
    if os.path.exists(pred_path):
        for g in util.read_json(pred_path).get("games", []):
            pm = {}
            for s in ("home", "away"):
                for pl in g.get("props", {}).get(s, []):
                    pm[norm(pl["player"])] = {p["stat"]: p["mu"] for p in pl["props"]}
            pred_index[(norm(g["home"]), norm(g["away"]))] = pm

    # Fetch each book's board once.
    book_events = {}
    for name, (lister, _) in BOOKS.items():
        evs = _safe(lister) or []
        book_events[name] = evs
        util.log(f"  [{name}] {len(evs)} events")

    books_present, out_games = set(), []
    for f in fxs:
        elo_wp = None
        if elo:
            elo_wp = ratings.elo_win_prob(elo, f["homeId"], f["awayId"], elo.get("_meta", {}).get("home_field", 24.0))
        dist = sim.distributions(f["home_team"], f["away_team"], f["home_sp"], f["away_sp"], f["park"], cfg, elo_home_wp=elo_wp)

        prop_mu = pred_index.get((norm(f["home"]), norm(f["away"])), {})
        sel_books, sel_label, sel_meta = {}, {}, {}
        prop_sel = {}  # sid -> {model, label, books}
        for book, (_, get_markets) in BOOKS.items():
            ev = _match_event(book_events[book], f["home"], f["away"])
            if not ev:
                continue
            for mname, sels in _safe(get_markets, ev) or []:
                for mkt, side, line, price, label in canon(mname, sels, f["home"], f["away"]):
                    sid = f"{mkt}|{side}|{line}"
                    sel_books.setdefault(sid, {})[book] = round(price, 2)
                    sel_label[sid] = label
                    sel_meta[sid] = (mkt, side, line)
                    books_present.add(book)
                # Individual player props, priced off the model's projection (Poisson over).
                for player, stat, ou, line, price in parse_player_prop(mname, sels):
                    mu = prop_mu.get(norm(player), {}).get(stat)
                    if mu is None:
                        continue
                    ov = sim.over_prob(mu, line)
                    model = ov if ou == "over" else 1 - ov
                    sid = f"prop|{norm(player)}|{stat}|{ou}|{line}"
                    cell = prop_sel.setdefault(sid, {"model": round(model, 4),
                                                     "label": f"{player} {ou.title()} {line:g} {stat}", "books": {}})
                    cell["books"][book] = round(price, 2)
                    books_present.add(book)
        if not sel_books and not prop_sel:
            continue

        markets = {}
        for sid, books in sel_books.items():
            mkt, side, line = sel_meta[sid]
            mp = model_prob(dist, mkt, side, line)
            if mp is None or mp <= 0:
                continue
            best_book = max(books, key=books.get)
            best = books[best_book]
            # A liquid MLB market never shows a ~40%+ edge either way; that gap signals a
            # stale / placeholder / deep-alt line or a model-calibration edge case, not
            # real value, so drop it (keeps the board credible in both directions).
            ev_chk = mp * best - 1
            if ev_chk > 0.40 or ev_chk < -0.45:
                continue
            markets.setdefault(mkt, {"key": mkt, "label": MARKET_LABEL.get(mkt, mkt), "selections": []})
            markets[mkt]["selections"].append({
                "id": sid, "label": sel_label[sid], "model": round(mp, 4),
                "fair": round(1 / mp, 2), "books": books,
                "best": {"price": best, "book": best_book},
                "ev": round(mp * best - 1, 4), "edge": round(mp - 1 / best, 4),
            })
        # Player props (model prob already computed). Keep BOTH sides a book offers (no
        # negative EV floor) so Pick'em can always show a real, offered side; only drop
        # the rare implausible positive edge.
        for sid, c in prop_sel.items():
            if not c["books"] or c["model"] <= 0:
                continue
            best_book = max(c["books"], key=c["books"].get)
            best = c["books"][best_book]
            ev_chk = c["model"] * best - 1
            if ev_chk > 0.45:
                continue
            markets.setdefault("prop", {"key": "prop", "label": MARKET_LABEL["prop"], "selections": []})
            markets["prop"]["selections"].append({
                "id": sid, "label": c["label"], "model": c["model"],
                "fair": round(1 / c["model"], 2), "books": c["books"],
                "best": {"price": best, "book": best_book},
                "ev": round(ev_chk, 4), "edge": round(c["model"] - 1 / best, 4),
            })

        ordered = [markets[k] for k in MARKET_ORDER if k in markets]
        for m in ordered:
            m["selections"].sort(key=lambda s: -s["ev"])
        if ordered:
            out_games.append({"home": f["home"], "away": f["away"], "date": f["date"],
                              "homeAbbr": f["homeAbbr"], "awayAbbr": f["awayAbbr"], "markets": ordered})

    out_games.sort(key=lambda g: g["date"])
    odds_path = util.abspath(os.path.join(dd, "odds.json"))
    # Don't downgrade a richer board: if this host reaches fewer books than the
    # existing file already has (e.g. a CI runner that can't see TAB/Dabble, or a
    # geo-block that returns nothing), keep what's there.
    existing_books = 0
    if os.path.exists(odds_path):
        try:
            existing_books = len(util.read_json(odds_path).get("books", []))
        except Exception:  # noqa: BLE001
            existing_books = 0
    if existing_books and len(books_present) < existing_books:
        util.log(f"odds: only {sorted(books_present)} reachable here ({existing_books} already on file) — keeping the existing odds.json")
    else:
        util.write_json(odds_path, {"generated": _now(), "books": sorted(books_present),
                                    "count": len(out_games), "games": out_games})
        util.log(f"odds: {len(out_games)} games priced across {sorted(books_present)}")

    # Dabble Pick'em (multiplier game) — its own MLB Pick'em competitions.
    _safe(dab_pickem)
    if _PICKEM:
        util.write_json(util.abspath(os.path.join(dd, "pickem-lines.json")), {"generated": _now(), "lines": _PICKEM})
        util.log(f"odds: {len(_PICKEM)} Dabble pick'em lines")

    return {"games": len(out_games), "books": sorted(books_present), "pickem": len(_PICKEM)}


def main(argv) -> int:
    return 0 if run(util.load_config()) is not None else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
