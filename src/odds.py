"""Odds — overlay bookmaker prices on the model book and surface value.

For every upcoming game we pull each book's MLB board, map the book's market and
selection naming to the canonical model markets (moneyline, run line, total runs,
first-5 total, team totals), read the model probability out of ``predictions.json``,
and compute fair price / best price / EV / edge. Output: ``docs/data/odds.json``.

Books: Sportsbet (public apigw JSON, no auth — moneyline / run line / totals / F5 /
team totals). Each book is wrapped so one failing never breaks the rest. The
Australian books geo-restrict to AU IPs, so this stage is best-effort and is meant
to run from a local AU cron (``scripts/odds-cron.sh``); the rest of the pipeline runs
anywhere.
"""
from __future__ import annotations

import math
import os
import re
import sys
import time
import unicodedata

from . import util

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
NUM = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def _get(url, retries=2, timeout=30):
    return util.http_get_json(url, retries=retries, timeout=timeout)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


# --------------------------------------------------------------------------- #
# Sportsbet (baseball = class 18)
# --------------------------------------------------------------------------- #
SB = "https://www.sportsbet.com.au/apigw/sportsbook-sports/Sportsbook/Sports"
SB_BASEBALL = 18
MLB_HINTS = ("mlb", "major league")


def sb_events():
    comps = _get(f"{SB}/{SB_BASEBALL}/Competitions") or []
    out = []
    for c in comps:
        name = (c.get("name") or "").lower()
        if not any(h in name for h in MLB_HINTS):
            continue
        d = _get(f"{SB}/Competitions/{c.get('id')}?displayType=default&eventFilter=matches")
        for e in (d or {}).get("events", []):
            if e.get("participant1") and e.get("participant2"):
                out.append({"id": e["id"], "home": e["participant2"], "away": e["participant1"]})
    return out


def sb_markets(ev):
    d = _get(f"{SB}/Events/{ev['id']}/Markets")
    out = []
    for m in (d if isinstance(d, list) else []):
        sels = []
        for s in m.get("selections", []):
            sels.append({
                "name": s.get("name", ""),
                "price": (s.get("price") or {}).get("winPrice"),
                "hcap": s.get("unformattedHandicap"),
                "disp": s.get("displayHandicap"),
            })
        out.append({"name": m.get("name", ""), "selections": sels})
    return out


BOOKS = {"sportsbet": (sb_events, sb_markets)}


# --------------------------------------------------------------------------- #
# Canonical market mapping  (book market+selection -> model selection id)
# --------------------------------------------------------------------------- #
def map_selections(market_name: str, selections: list[dict], home: str, away: str):
    """Yield (model_market, model_side, line, book_price, label)."""
    low = market_name.lower()
    nh, na = norm(home), norm(away)

    def side_of(sel_name):
        n = norm(sel_name)
        if n == nh or (len(nh) >= 4 and nh in n):
            return "home"
        if n == na or (len(na) >= 4 and na in n):
            return "away"
        return None

    out = []
    if low == "money line":
        for s in selections:
            side = side_of(s["name"])
            if side and s["price"]:
                out.append(("ml", side, None, float(s["price"]), home if side == "home" else away))
    elif low == "run line":
        for s in selections:
            side = side_of(s["name"])
            if side and s["price"]:
                hc = util.num(s.get("hcap"), 1.5)
                out.append(("rl", side, hc, float(s["price"]),
                            f"{home if side == 'home' else away} {s.get('disp', '')}"))
    elif low == "total runs":
        for s in selections:
            ou = "over" if "over" in s["name"].lower() else "under" if "under" in s["name"].lower() else None
            if ou and s["price"]:
                out.append(("total", ou, util.num(s.get("hcap")), float(s["price"]),
                            f"{ou.title()} {s.get('hcap')}"))
    elif low == "1st 5 innings total runs":
        for s in selections:
            ou = "over" if "over" in s["name"].lower() else "under" if "under" in s["name"].lower() else None
            if ou and s["price"]:
                out.append(("f5_total", ou, util.num(s.get("hcap")), float(s["price"]),
                            f"F5 {ou.title()} {s.get('hcap')}"))
    return out


def model_prob(game: dict, mkt: str, side: str, line) -> float | None:
    """Read the model probability for a canonical selection from a prediction."""
    if mkt == "ml":
        return game["win_home"] if side == "home" else game["win_away"]
    markets = {m["key"]: m for m in game.get("markets", [])}
    if mkt == "rl":
        rl = markets.get("rl")
        if not rl:
            return None
        for s in rl["selections"]:
            if s["label"].startswith("home" if side == "home" else "away"):
                return s["prob"]
    if mkt in ("total", "f5_total"):
        key = "total" if mkt == "total" else "f5_total"
        m = markets.get(key)
        if not m:
            return None
        for ln in m.get("lines", []):
            if abs(ln["line"] - (line or 0)) < 1e-6:
                return ln["over"] if side == "over" else ln["under"]
    return None


# --------------------------------------------------------------------------- #
def _index_predictions(preds: dict) -> dict:
    idx = {}
    for g in preds.get("games", []):
        idx[(norm(g["home"]), norm(g["away"]))] = g
    return idx


def _match_game(idx: dict, home: str, away: str):
    nh, na = norm(home), norm(away)
    for (h, a), g in idx.items():
        if (nh in h or h in nh) and (na in a or a in na):
            return g
    return None


def run(cfg: dict) -> dict:
    pred_path = util.abspath(os.path.join(cfg["paths"]["docs_data_dir"], "predictions.json"))
    if not os.path.exists(pred_path):
        util.log("odds: no predictions.json; run predict first")
        return {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "books": [], "games": []}
    idx = _index_predictions(util.read_json(pred_path))

    # game key -> {market+side+line -> {books:{}, model, ...}}
    games: dict = {}
    used_books = []
    for book, (list_events, get_markets) in BOOKS.items():
        try:
            evs = list_events()
        except Exception as exc:  # noqa: BLE001
            util.log(f"odds: {book} events failed ({exc})")
            continue
        if evs:
            used_books.append(book)
        for ev in evs:
            g = _match_game(idx, ev["home"], ev["away"])
            if not g:
                continue
            try:
                mkts = get_markets(ev)
            except Exception as exc:  # noqa: BLE001
                util.log(f"odds: {book} markets failed ({exc})")
                continue
            gk = (g["home"], g["away"])
            bucket = games.setdefault(gk, {"home": g["home"], "away": g["away"], "date": g["date"],
                                           "homeAbbr": g["homeAbbr"], "awayAbbr": g["awayAbbr"], "sels": {}})
            for m in mkts:
                for mkt, side, line, price, label in map_selections(m["name"], m["selections"], g["home"], g["away"]):
                    mp = model_prob(g, mkt, side, line)
                    if mp is None:
                        continue
                    sid = f"{mkt}|{side}|{line}"
                    cell = bucket["sels"].setdefault(sid, {"market": mkt, "label": label, "model": round(mp, 4),
                                                           "fair": round(1 / mp, 3) if mp else None, "books": {}})
                    cell["books"][book] = price

    # Compute best price + EV + edge per selection.
    out_games = []
    for gk, b in games.items():
        sels = []
        for sid, c in b["sels"].items():
            if not c["books"]:
                continue
            best_book = max(c["books"], key=c["books"].get)
            best = c["books"][best_book]
            ev = round(c["model"] * best - 1, 4)
            sels.append({**c, "best": {"book": best_book, "price": best}, "ev": ev,
                         "edge": round(c["model"] - 1 / best, 4)})
        if sels:
            sels.sort(key=lambda s: -s["ev"])
            out_games.append({"home": b["home"], "away": b["away"], "date": b["date"],
                              "homeAbbr": b["homeAbbr"], "awayAbbr": b["awayAbbr"], "selections": sels})

    out_games.sort(key=lambda g: g["date"])
    result = {
        "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "books": used_books, "count": len(out_games), "games": out_games,
    }
    util.write_json(util.abspath(os.path.join(cfg["paths"]["docs_data_dir"], "odds.json")), result)
    util.log(f"odds: {len(out_games)} games priced across books={used_books}")
    return result


def main(argv) -> int:
    return 0 if run(util.load_config()) is not None else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
