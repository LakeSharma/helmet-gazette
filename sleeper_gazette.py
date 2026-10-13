#!/usr/bin/env python3
"""
Sleeper Gazette: a weekly newspaper for your Sleeper fantasy football league.

Run it once a week, ideally Tuesday after Monday Night Football:

    python sleeper_gazette.py --league 1234567890

On the first run it backfills every completed week of this season and up to
five prior seasons, so rivalries and the all-time series start out populated.
Everything is saved in gazette/data/archive-<league>.json. That file is the
paper's memory: results, trades, pickups, and every past edition's headlines
and storylines, which later editions refer back to. Don't delete it.

Writing:
  * With ANTHROPIC_API_KEY set, Claude writes the articles from the week's
    facts plus the storylines from previous editions.
  * Without it, the paper is written from built-in templates.

Output: gazette/<season>-week-NN.html for each edition, plus gazette/index.html.

Needs only the Python 3.9+ standard library.
"""

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- settings
LEAGUE_ID = "1308971356657840128"      # Your Sleeper league ID (the long number in the league URL)
PAPER_NAME = ""     # Leave blank for "The <League Name> Gazette"
VOICE = ("a sharp, funny small-town sports editor who loves a grudge match and "
         "roasts bad lineup decisions, but is never mean-spirited")
# Named rivalries, using Sleeper display names: ("name1", "name2", "Game name")
RIVALRIES = [
    # ("lake", "mike", "The Backyard Bowl"),
]
MODEL = os.environ.get("GAZETTE_MODEL", "claude-sonnet-5-5")
# --------------------------------------------------------------------------

SLEEPER = "https://api.sleeper.app/v1"
PLAYERS = {}


def get(path, retries=3):
    url = path if path.startswith("http") else SLEEPER + path
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sleeper-gazette/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))


def r2(x):
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def fmt(x):
    return f"{x:.2f}"


def ordinal(n):
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def roman(n):
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


# ----------------------------------------------------------------- players
def load_players(data_dir):
    path = data_dir / "players.json"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < 7 * 86400
    if not fresh:
        print("Downloading the Sleeper player list (about 5 MB, refreshed weekly)...")
        try:
            raw = get("/players/nfl") or {}
            slim = {}
            for pid, p in raw.items():
                name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                slim[pid] = [name or pid, p.get("position") or "", p.get("team") or ""]
            path.write_text(json.dumps(slim), encoding="utf-8")
        except Exception as e:
            print(f"  Couldn't refresh the player list ({e}); using the cached copy if there is one.")
    if path.exists():
        PLAYERS.update(json.loads(path.read_text(encoding="utf-8")))


def pname(pid):
    p = PLAYERS.get(pid)
    return p[0] if p else str(pid)


def ppos(pid):
    p = PLAYERS.get(pid)
    if p:
        return p[1]
    return "DEF" if str(pid).isalpha() else ""


def pteam(pid):
    p = PLAYERS.get(pid)
    return p[2] if p else ""


# ------------------------------------------------------------------ syncing
def slim_matchups(ms):
    out = []
    for m in ms or []:
        pts = m.get("custom_points")
        if pts is None:
            pts = m.get("points")
        out.append({
            "roster_id": m.get("roster_id"),
            "matchup_id": m.get("matchup_id"),
            "points": r2(pts),
            "starters": [p for p in (m.get("starters") or []) if p and p != "0"],
            "players": m.get("players") or [],
            "pp": {k: r2(v) for k, v in (m.get("players_points") or {}).items()},
        })
    return out


def slim_tx(txs):
    out = []
    for t in txs or []:
        if t.get("status") != "complete":
            continue
        out.append({
            "id": t.get("transaction_id"),
            "type": t.get("type"),
            "created": t.get("created"),
            "adds": t.get("adds") or {},
            "drops": t.get("drops") or {},
            "roster_ids": t.get("roster_ids") or [],
            "picks": [{"season": p.get("season"), "round": p.get("round"),
                       "to": p.get("owner_id"), "from": p.get("previous_owner_id")}
                      for p in (t.get("draft_picks") or [])],
            "bid": (t.get("settings") or {}).get("waiver_bid"),
        })
    return out


def has_points(ms):
    return any((m.get("points") or 0) > 0 for m in ms or [])


def sync_league(arc, league_id, through_week=None, force=()):
    """Fetch one season. through_week=None means walk until the season runs out."""
    lg = get(f"/league/{league_id}")
    if not lg:
        sys.exit(f"Sleeper didn't recognize league {league_id}. Check the ID in your league URL.")
    season = str(lg["season"])
    s = arc["seasons"].setdefault(season, {"weeks": {}})
    if s.get("complete") and through_week is None:
        return lg
    users = get(f"/league/{league_id}/users") or []
    rosters = get(f"/league/{league_id}/rosters") or []
    s.update(league_id=league_id, name=lg.get("name") or "Our League",
             playoff_week_start=(lg.get("settings") or {}).get("playoff_week_start") or 0)
    s["owners"] = {u["user_id"]: {"name": u.get("display_name") or "Unknown",
                                  "team": (u.get("metadata") or {}).get("team_name") or u.get("display_name") or "Unknown"}
                   for u in users}
    s["roster_owner"] = {str(r["roster_id"]): (r.get("owner_id") or f"roster-{r['roster_id']}") for r in rosters}
    for rid, oid in s["roster_owner"].items():
        s["owners"].setdefault(oid, {"name": f"Team {rid}", "team": f"Team {rid}"})

    last = through_week or 18
    for w in range(1, last + 1):
        if str(w) in s["weeks"] and w not in force:
            continue
        ms = get(f"/league/{league_id}/matchups/{w}") or []
        if not has_points(ms):
            if through_week is None:
                break
            continue
        tx = get(f"/league/{league_id}/transactions/{w}") or []
        s["weeks"][str(w)] = {"matchups": slim_matchups(ms), "transactions": slim_tx(tx)}
        print(f"  {season} week {w} saved")
    if lg.get("status") == "complete":
        s["complete"] = True
    return lg


# --------------------------------------------------------------- analysis
def owner_of(s, rid):
    return s["roster_owner"].get(str(rid), f"roster-{rid}")


def is_playoffs(s, w):
    p = s.get("playoff_week_start") or 0
    return bool(p) and w >= p


def week_games(s, season, w):
    wk = s["weeks"].get(str(w))
    if not wk:
        return []
    groups = defaultdict(list)
    for m in wk["matchups"]:
        if m.get("matchup_id") is not None:
            groups[m["matchup_id"]].append(m)
    out = []
    for mid in sorted(groups):
        ms = groups[mid]
        if len(ms) != 2:
            continue
        a, b = ms
        out.append({"season": season, "week": w, "mid": mid,
                    "a": owner_of(s, a["roster_id"]), "b": owner_of(s, b["roster_id"]),
                    "ma": a, "mb": b, "pa": a["points"], "pb": b["points"],
                    "playoffs": is_playoffs(s, w)})
    return out


def all_games(arc):
    out = []
    for season in sorted(arc["seasons"], key=int):
        s = arc["seasons"][season]
        for w in sorted(int(k) for k in s["weeks"]):
            out += week_games(s, season, w)
    return out


def gkey(season, week):
    return (int(season), int(week))


def streak_of(results):
    if not results:
        return ""
    last, n = results[-1], 0
    for r in reversed(results):
        if r != last:
            break
        n += 1
    return f"{last}{n}"


def standings(arc, season, through):
    s = arc["seasons"][season]
    blank = lambda oid: {"oid": oid, "w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0, "res": []}
    rec = {oid: blank(oid) for oid in set(s["roster_owner"].values())}
    for w in range(1, through + 1):
        for g in week_games(s, season, w):
            if g["playoffs"]:
                continue
            for me, pm, po in ((g["a"], g["pa"], g["pb"]), (g["b"], g["pb"], g["pa"])):
                r = rec.setdefault(me, blank(me))
                r["pf"] += pm
                r["pa"] += po
                res = "W" if pm > po else "L" if pm < po else "T"
                r[{"W": "w", "L": "l", "T": "t"}[res]] += 1
                r["res"].append(res)
    rows = sorted(rec.values(), key=lambda r: (-(r["w"] + 0.5 * r["t"]), -r["pf"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["pf"], r["pa"] = round(r["pf"], 2), round(r["pa"], 2)
        r["streak"] = streak_of(r["res"])
    return rows


def series(allg, a, b, upto):
    """All-time head-to-head between owners a and b, up to and including `upto`."""
    wa = wb = t = 0
    meetings = []
    for g in allg:
        if gkey(g["season"], g["week"]) > upto or {g["a"], g["b"]} != {a, b}:
            continue
        pa, pb = (g["pa"], g["pb"]) if g["a"] == a else (g["pb"], g["pa"])
        if pa > pb:
            wa += 1
        elif pb > pa:
            wb += 1
        else:
            t += 1
        meetings.append({"season": g["season"], "week": g["week"], "a_pts": pa, "b_pts": pb})
    return wa, wb, t, meetings


def started_points(s, rid, pids, from_w, to_w, only_week=None):
    total, starts = 0.0, 0
    weeks = [only_week] if only_week else range(from_w, to_w + 1)
    for w in weeks:
        wk = s["weeks"].get(str(w))
        if not wk:
            continue
        for m in wk["matchups"]:
            if m["roster_id"] != rid:
                continue
            for p in pids:
                if p in m["starters"]:
                    total += m["pp"].get(p, 0)
                    starts += 1
    return round(total, 2), starts


def bench_blunder(m):
    starters = set(m["starters"])
    best = None
    for bp in m["players"]:
        if bp in starters:
            continue
        pos = ppos(bp)
        cands = [p for p in m["starters"] if ppos(p) == pos]
        if not pos or not cands:
            continue
        worst = min(cands, key=lambda p: m["pp"].get(p, 0))
        gain = m["pp"].get(bp, 0) - m["pp"].get(worst, 0)
        if gain > 0 and (best is None or gain > best["gain"]):
            best = {"bench": pname(bp), "bench_pts": m["pp"].get(bp, 0),
                    "starter": pname(worst), "starter_pts": m["pp"].get(worst, 0), "gain": round(gain, 2)}
    return best


def top_starters(m, n=3):
    ranked = sorted(m["starters"], key=lambda p: -m["pp"].get(p, 0))[:n]
    return [{"name": pname(p), "pos": ppos(p), "nfl": pteam(p), "pts": m["pp"].get(p, 0)} for p in ranked]


def pick_label(p):
    return f"{p['season']} {ordinal(p['round'])}-round pick"


def compute_facts(arc, season, week, paper):
    s = arc["seasons"][season]

    def team(oid):
        for key in [season] + sorted(arc["seasons"], key=int, reverse=True):
            o = arc["seasons"][key]["owners"].get(oid)
            if o:
                return o["team"]
        return "A former manager"

    def manager(oid):
        for key in [season] + sorted(arc["seasons"], key=int, reverse=True):
            o = arc["seasons"][key]["owners"].get(oid)
            if o:
                return o["name"]
        return "Unknown"

    allg = all_games(arc)
    games = week_games(s, season, week)
    now = gkey(season, week)
    st = standings(arc, season, week)
    prev_st = {r["oid"]: r["rank"] for r in standings(arc, season, week - 1)} if week > 1 else {}

    # Configured rivalries, matched by display name
    by_name = {}
    for sk in arc["seasons"].values():
        for oid, o in sk["owners"].items():
            by_name[o["name"].lower()] = oid
    named_rivals = {}
    for n1, n2, title in RIVALRIES:
        o1, o2 = by_name.get(n1.lower()), by_name.get(n2.lower())
        if o1 and o2:
            named_rivals[frozenset((o1, o2))] = title

    gfacts, everyone = [], []
    for g in games:
        wa, wb, t, meets = series(allg, g["a"], g["b"], now)
        tie = g["pa"] == g["pb"]
        a_won = g["pa"] >= g["pb"]
        W, L = (g["a"], g["b"]) if a_won else (g["b"], g["a"])
        mW, mL = (g["ma"], g["mb"]) if a_won else (g["mb"], g["ma"])
        wins_W, wins_L = (wa, wb) if a_won else (wb, wa)
        prior = [m for m in meets if gkey(m["season"], m["week"]) < now]
        last = None
        if prior:
            m = prior[-1]
            pW, pL = (m["a_pts"], m["b_pts"]) if g["a"] == W else (m["b_pts"], m["a_pts"])
            last = {"season": m["season"], "week": m["week"], "winner": team(W) if pW > pL else team(L),
                    "score": f"{fmt(max(pW, pL))}–{fmt(min(pW, pL))}"}
        upset = None
        if prev_st and not tie:
            rw, rl = prev_st.get(W), prev_st.get(L)
            if rw and rl and rw - rl >= 3:
                upset = {"winner_rank": rw, "loser_rank": rl}
        blunders = []
        for oid, m, lost_by in ((W, mW, 0), (L, mL, round(abs(g["pa"] - g["pb"]), 2))):
            b = bench_blunder(m)
            if b:
                b["team"] = team(oid)
                b["cost_game"] = bool(lost_by) and b["gain"] > lost_by
                blunders.append(b)
        gfacts.append({
            "mid": g["mid"], "tie": tie,
            "winner": team(W), "loser": team(L), "winner_oid": W, "loser_oid": L,
            "winner_manager": manager(W), "loser_manager": manager(L),
            "wp": max(g["pa"], g["pb"]), "lp": min(g["pa"], g["pb"]),
            "margin": round(abs(g["pa"] - g["pb"]), 2),
            "winner_top": top_starters(mW), "loser_top": top_starters(mL),
            "series": {"meetings": len(meets), "winner_wins": wins_W, "loser_wins": wins_L, "ties": t},
            "last_meeting": last, "rivalry": named_rivals.get(frozenset((W, L))),
            "upset": upset, "blunders": blunders,
        })
        everyone += [(g["a"], g["pa"], g["ma"]), (g["b"], g["pb"], g["mb"])]

    awards = {}
    if everyone:
        hi = max(everyone, key=lambda x: x[1])
        lo = min(everyone, key=lambda x: x[1])
        awards["high"] = {"team": team(hi[0]), "pts": hi[1]}
        awards["low"] = {"team": team(lo[0]), "pts": lo[1]}
        starters = [(oid, p, m["pp"].get(p, 0)) for oid, _, m in everyone for p in m["starters"]]
        if starters:
            oid, p, pts = max(starters, key=lambda x: x[2])
            awards["player_of_week"] = {"player": pname(p), "pos": ppos(p), "team": team(oid), "pts": pts}
            skill = [x for x in starters if ppos(x[1]) in ("QB", "RB", "WR", "TE")]
            if skill:
                oid, p, pts = min(skill, key=lambda x: x[2])
                awards["dud"] = {"player": pname(p), "pos": ppos(p), "team": team(oid), "pts": pts}
        allb = [b for gf in gfacts for b in gf["blunders"]]
        if allb:
            awards["bench_blunder"] = max(allb, key=lambda b: (b["cost_game"], b["gain"]))
        # All-play: how many teams each score would have beaten this week
        allplay = {oid: sum(1 for o2, p2, _ in everyone if o2 != oid and pts > p2) for oid, pts, _ in everyone}
        losers = [(gf["loser_oid"], gf["lp"]) for gf in gfacts if not gf["tie"]]
        winners = [(gf["winner_oid"], gf["wp"]) for gf in gfacts if not gf["tie"]]
        n = len(everyone) - 1
        if losers:
            oid, pts = max(losers, key=lambda x: x[1])
            if allplay[oid] >= n / 2:
                awards["bad_beat"] = {"team": team(oid), "pts": pts, "would_beat": allplay[oid], "of": n}
        if winners:
            oid, pts = min(winners, key=lambda x: x[1])
            if allplay[oid] < n / 2:
                awards["lucky_win"] = {"team": team(oid), "pts": pts, "would_beat": allplay[oid], "of": n}
    if gfacts:
        decided = [g for g in gfacts if not g["tie"]]
        if decided:
            awards["blowout"] = max(decided, key=lambda g: g["margin"])["mid"]
            awards["nailbiter"] = min(decided, key=lambda g: g["margin"])["mid"]

    # Records
    records = []
    if everyone:
        hi_pts = awards["high"]["pts"]
        prev_scores = [p for g in allg if gkey(g["season"], g["week"]) < now for p in (g["pa"], g["pb"])]
        season_prev = [p for g in allg if g["season"] == season and g["week"] < week for p in (g["pa"], g["pb"])]
        if prev_scores and hi_pts > max(prev_scores):
            records.append(f"{awards['high']['team']} set the all-time league record with {fmt(hi_pts)} points (old mark {fmt(max(prev_scores))}).")
        elif season_prev and hi_pts > max(season_prev):
            records.append(f"{awards['high']['team']}'s {fmt(hi_pts)} is the highest score of the {season} season.")
        lo_pts = awards["low"]["pts"]
        if prev_scores and lo_pts < min(prev_scores):
            records.append(f"{awards['low']['team']}'s {fmt(lo_pts)} is the lowest score in league history.")

    # Standings with movement
    stand = []
    for r in st:
        mv = prev_st.get(r["oid"], r["rank"]) - r["rank"] if prev_st else 0
        stand.append({"rank": r["rank"], "team": team(r["oid"]), "manager": manager(r["oid"]),
                      "w": r["w"], "l": r["l"], "t": r["t"], "pf": r["pf"], "pa": r["pa"],
                      "streak": r["streak"], "move": mv})
    streaks = []
    for r in st:
        n = int(r["streak"][1:] or 0) if r["streak"] else 0
        if n >= 3 and r["streak"][0] in "WL":
            word = "won" if r["streak"][0] == "W" else "lost"
            streaks.append(f"{team(r['oid'])} has {word} {n} straight.")
    if week >= 3:
        for r in st:
            if r["l"] == 0 and r["w"] == week:
                streaks.append(f"{team(r['oid'])} is still unbeaten at {r['w']}-0.")
            if r["w"] == 0 and r["l"] == week:
                streaks.append(f"{team(r['oid'])} is still looking for its first win (0-{r['l']}).")

    # Transactions
    this_wk = s["weeks"].get(str(week), {}).get("transactions", [])
    moves = {"trades": [], "adds": []}
    for t in this_wk:
        if t["type"] == "trade":
            sides = []
            for rid in t["roster_ids"]:
                got = [pname(p) for p, to in t["adds"].items() if to == rid]
                got += [pick_label(p) for p in t["picks"] if p["to"] == rid]
                sides.append({"team": team(owner_of(s, rid)), "got": got})
            moves["trades"].append({"sides": sides})
        elif t["type"] in ("waiver", "free_agent"):
            for p, rid in t["adds"].items():
                dropped = [pname(d) for d, frm in t["drops"].items() if frm == rid]
                moves["adds"].append({"team": team(owner_of(s, rid)), "player": pname(p), "pos": ppos(p),
                                      "via": "waivers" if t["type"] == "waiver" else "free agency",
                                      "bid": t.get("bid"), "dropped": dropped})

    trade_tracker, pickups = [], []
    for wk in sorted(int(k) for k in s["weeks"]):
        if wk > week:
            continue
        for t in s["weeks"][str(wk)]["transactions"]:
            if t["type"] == "trade":
                sides = []
                for rid in t["roster_ids"]:
                    got = [p for p, to in t["adds"].items() if to == rid]
                    pts, _ = started_points(s, rid, got, wk, week)
                    sides.append({"team": team(owner_of(s, rid)),
                                  "got": [pname(p) for p in got] + [pick_label(p) for p in t["picks"] if p["to"] == rid],
                                  "pts": pts})
                lead = max(sides, key=lambda x: x["pts"]) if sides else None
                gap = round(lead["pts"] - min(x["pts"] for x in sides), 2) if sides else 0
                trade_tracker.append({"week": wk, "sides": sides,
                                      "leader": lead["team"] if lead and gap > 0 else None, "gap": gap})
            elif t["type"] in ("waiver", "free_agent"):
                for p, rid in t["adds"].items():
                    pts, starts = started_points(s, rid, [p], wk, week)
                    this_pts, _ = started_points(s, rid, [p], wk, week, only_week=week)
                    pickups.append({"player": pname(p), "pos": ppos(p), "team": team(owner_of(s, rid)),
                                    "week_added": wk, "bid": t.get("bid"), "pts": pts, "starts": starts,
                                    "this_week": this_pts})
    best = {}
    for p in pickups:  # a player added twice by the same team counts once
        k = (p["player"], p["team"])
        if k not in best or p["pts"] > best[k]["pts"]:
            best[k] = p
    hof = sorted((p for p in best.values() if p["pts"] > 0), key=lambda p: -p["pts"])[:6]
    wonder = max((p for p in pickups if p["this_week"] > 0), key=lambda p: p["this_week"], default=None)

    # Rivalry watch: the most-played, most even series in the archive
    pairs = defaultdict(list)
    for g in allg:
        if gkey(g["season"], g["week"]) <= now:
            pairs[frozenset((g["a"], g["b"]))].append(g)
    rivalry_watch = []
    for pair, gl in pairs.items():
        if len(pair) != 2:
            continue
        a, b = sorted(pair)
        wa, wb, t, meets = series(allg, a, b, now)
        if len(meets) < 2 and frozenset((a, b)) not in named_rivals:
            continue
        avg_margin = sum(abs(m["a_pts"] - m["b_pts"]) for m in meets) / len(meets)
        score = len(meets) * 3 - abs(wa - wb) * 4 - avg_margin / 10 + (50 if frozenset((a, b)) in named_rivals else 0)
        lead_t, lead_w, trail_w = (team(a), wa, wb) if wa >= wb else (team(b), wb, wa)
        rivalry_watch.append({"teams": [team(a), team(b)], "title": named_rivals.get(frozenset((a, b))),
                              "meetings": len(meets), "leader": lead_t if lead_w != trail_w else None,
                              "record": f"{lead_w}-{trail_w}" + (f"-{t}" if t else ""),
                              "avg_margin": round(avg_margin, 2), "_score": score})
    rivalry_watch.sort(key=lambda r: -r["_score"])
    for r in rivalry_watch:
        r.pop("_score")
    rivalry_watch = rivalry_watch[:3]

    one_year_ago = None
    last_season = str(int(season) - 1)
    if last_season in arc["seasons"]:
        old = week_games(arc["seasons"][last_season], last_season, week)
        if old:
            g = max(old, key=lambda g: max(g["pa"], g["pb"]))
            W, L = (g["a"], g["b"]) if g["pa"] >= g["pb"] else (g["b"], g["a"])
            one_year_ago = (f"Week {week}, {last_season}: {team(W)} beat {team(L)} "
                            f"{fmt(max(g['pa'], g['pb']))}–{fmt(min(g['pa'], g['pb']))}, the week's top score.")

    previous = []
    for k in sorted(arc.get("editions", {}), key=lambda k: gkey(*k.split("-")), reverse=True):
        if gkey(*k.split("-")) >= now:
            continue
        e = arc["editions"][k]
        previous.append({"edition": f"Week {e['week']}, {e['season']}", "headline": e["written"].get("headline"),
                         "storylines": e["written"].get("storylines", [])})
        if len(previous) == 4:
            break

    return {"paper": paper, "league": s["name"], "season": season, "week": week,
            "playoffs": is_playoffs(s, week), "games": gfacts, "standings": stand,
            "awards": awards, "records": records, "streaks": streaks, "moves": moves,
            "trade_tracker": trade_tracker, "pickup_hof": hof, "waiver_wonder": wonder,
            "rivalry_watch": rivalry_watch, "one_year_ago": one_year_ago, "previous": previous}


# ----------------------------------------------------------------- writing
def recap_verb(margin, rnd):
    if margin < 3:
        return rnd.choice(["edges", "squeaks past", "survives"])
    if margin < 10:
        return rnd.choice(["holds off", "gets by", "outlasts"])
    if margin < 25:
        return rnd.choice(["beats", "takes care of", "tops"])
    if margin < 45:
        return rnd.choice(["handles", "pulls away from", "dispatches"])
    return rnd.choice(["routs", "buries", "flattens"])


def series_line(g):
    sr = g["series"]
    if sr["meetings"] <= 1:
        return "It was the first meeting between these two."
    w, l = sr["winner_wins"], sr["loser_wins"]
    rec = f"{max(w, l)}-{min(w, l)}" + (f"-{sr['ties']}" if sr["ties"] else "")
    if w == l:
        return f"The all-time series is now even at {rec}."
    who = g["winner"] if w > l else g["loser"]
    return f"{who} leads the all-time series {rec}."


def template_write(f):
    rnd = random.Random(f"{f['season']}-{f['week']}")
    games = {g["mid"]: g for g in f["games"]}
    aw = f["awards"]
    recaps = {}
    for g in f["games"]:
        if g["tie"]:
            hed = f"{g['winner']} and {g['loser']} tie at {fmt(g['wp'])}"
        else:
            hed = f"{g['winner']} {recap_verb(g['margin'], rnd)} {g['loser']}, {fmt(g['wp'])}–{fmt(g['lp'])}"
        body = []
        if g["winner_top"]:
            t = g["winner_top"][0]
            body.append(f"{t['name']} led {g['winner']} with {fmt(t['pts'])} points.")
        if g["loser_top"]:
            t = g["loser_top"][0]
            body.append(f"{t['name']}'s {fmt(t['pts'])} weren't enough for {g['loser']}.")
        for b in g["blunders"]:
            if b["cost_game"]:
                body.append(f"{b['team']} left {b['bench']} ({fmt(b['bench_pts'])}) on the bench while "
                            f"{b['starter']} scored {fmt(b['starter_pts'])}. That swap alone would have flipped the result.")
        if g["upset"]:
            body.append(f"It's an upset: {g['winner']} came in ranked {ordinal(g['upset']['winner_rank'])}, "
                        f"{g['loser']} {ordinal(g['upset']['loser_rank'])}.")
        if g["rivalry"]:
            body.append(f"{g['winner']} takes this year's edition of {g['rivalry']}.")
        body.append(series_line(g))
        recaps[str(g["mid"])] = {"headline": hed, "body": " ".join(body)}

    # Front page: pick the biggest story
    hed, dek, focus = None, None, None
    upsets = [g for g in f["games"] if g["upset"]]
    rival_games = [g for g in f["games"] if g["rivalry"]]
    costly = [g for g in f["games"] if any(b["cost_game"] for b in g["blunders"])]
    if f["records"] and "all-time" in f["records"][0]:
        hed = f"{aw['high']['team']} rewrites the record book with {fmt(aw['high']['pts'])}"
        dek = f["records"][0]
        focus = next((g for g in f["games"] if g["winner"] == aw["high"]["team"]), None)
    elif upsets:
        g = max(upsets, key=lambda g: g["upset"]["winner_rank"] - g["upset"]["loser_rank"])
        focus = g
        hed = f"{g['winner']} stuns {ordinal(g['upset']['loser_rank'])}-place {g['loser']}"
        dek = f"The {ordinal(g['upset']['winner_rank'])}-place team won {fmt(g['wp'])}–{fmt(g['lp'])}."
    elif rival_games:
        g = rival_games[0]
        focus = g
        hed = f"{g['winner']} claims {g['rivalry']}"
        dek = f"{fmt(g['wp'])}–{fmt(g['lp'])} over {g['loser']}. {series_line(g)}"
    elif aw.get("blowout") is not None and games[aw["blowout"]]["margin"] >= 40:
        g = games[aw["blowout"]]
        focus = g
        hed = f"{g['winner']} {recap_verb(g['margin'], rnd)} {g['loser']} by {fmt(g['margin'])}"
        dek = f"The final was {fmt(g['wp'])}–{fmt(g['lp'])}, the most lopsided game of the week."
    elif costly:
        g = costly[0]
        b = next(b for b in g["blunders"] if b["cost_game"])
        focus = g
        hed = f"{b['bench']} watches from the bench as {b['team']} falls"
        dek = f"{fmt(b['bench_pts'])} points sat unused in a {fmt(g['margin'])}-point loss."
    elif aw.get("nailbiter") is not None and games[aw["nailbiter"]]["margin"] < 3:
        g = games[aw["nailbiter"]]
        focus = g
        hed = f"{g['winner']} survives {g['loser']} by {fmt(g['margin'])}"
        dek = f"The closest finish of week {f['week']} ended {fmt(g['wp'])}–{fmt(g['lp'])}."
    elif aw.get("high"):
        hed = f"{aw['high']['team']} leads week {f['week']} with {fmt(aw['high']['pts'])}"
        dek = "The top score of the week, and a statement to the rest of the league."
    else:
        hed, dek = f"Week {f['week']} is in the books", "Results from around the league."

    lead = []
    if focus:
        lead.append(f"{recaps[str(focus['mid'])]['body']}")
    if aw.get("high"):
        p = aw.get("player_of_week")
        txt = f"{aw['high']['team']} posted the week's top score at {fmt(aw['high']['pts'])}"
        txt += f", while {aw['low']['team']} brought up the rear with {fmt(aw['low']['pts'])}." if aw.get("low") else "."
        if p:
            txt += f" {p['player']} ({p['pos']}, {p['team']}) was the best starter in the league with {fmt(p['pts'])}."
        lead.append(txt)
    if f["standings"]:
        top = f["standings"][0]
        txt = f"{top['team']} sits atop the standings at {top['w']}-{top['l']}" + (f"-{top['t']}" if top["t"] else "") + "."
        if f["streaks"]:
            txt += " " + " ".join(f["streaks"][:3])
        lead.append(txt)
    if f["moves"]["trades"]:
        tr = f["moves"]["trades"][0]
        lead.append("On the trade front: " + "; ".join(
            f"{sd['team']} gets {', '.join(sd['got']) or 'nothing'}" for sd in tr["sides"]) + ".")
    elif f["waiver_wonder"]:
        w = f["waiver_wonder"]
        lead.append(f"Off the wire, {w['player']} gave {w['team']} {fmt(w['this_week'])} points this week.")
    if f["previous"] and f["previous"][0].get("headline"):
        lead.append(f"Last week's front page: \u201c{f['previous'][0]['headline']}.\u201d The league has moved on, mostly.")

    col = []
    if aw.get("bad_beat"):
        b = aw["bad_beat"]
        col.append(f"Spare a thought for {b['team']}. {fmt(b['pts'])} points would have beaten {b['would_beat']} "
                   f"of {b['of']} teams this week, and it still went down as a loss.")
    if aw.get("lucky_win"):
        l = aw["lucky_win"]
        col.append(f"Meanwhile {l['team']} won with {fmt(l['pts'])}, a score that beats only {l['would_beat']} "
                   f"of {l['of']} teams. Nobody hands back a win, but nobody should brag about this one either.")
    if aw.get("dud"):
        d = aw["dud"]
        col.append(f"And to whoever started {d['player']}: {fmt(d['pts'])} points. {d['team']} will want that one back.")
    if not col:
        col.append("A quiet week in the press box. That never lasts.")

    storylines = list(f["records"]) + list(f["streaks"])
    for g in upsets[:1]:
        storylines.append(f"{g['winner']} upset {g['loser']} in week {f['week']}.")
    for t in f["trade_tracker"]:
        if t["leader"] and t["gap"] >= 15:
            storylines.append(f"{t['leader']} is winning the week-{t['week']} trade by {fmt(t['gap'])} points.")
    if f["pickup_hof"]:
        p = f["pickup_hof"][0]
        storylines.append(f"{p['player']} is the best pickup of the season so far ({p['team']}, {fmt(p['pts'])} pts).")

    return {"headline": hed, "subhead": dek, "lead": lead, "recaps": recaps,
            "column": {"title": "From the press box", "body": col}, "storylines": storylines[:6]}


def ai_write(facts, model, key):
    system = f"""You are the editor-in-chief of "{facts['paper']}", the weekly newspaper of a fantasy football league. Your voice: {VOICE}.

Rules:
- Use only the facts provided. Never invent players, scores, stats, quotes, trades, or events. Every number you write must appear in the facts.
- Call teams by their team names; mention managers by name now and then.
- Build continuity: use "previous" (earlier editions' headlines and storylines), "rivalry_watch", "trade_tracker", "pickup_hof" and "one_year_ago" to call back to running feuds, trade verdicts, and earlier predictions when the facts support it.
- Point totals are fantasy points. Keep two decimals when quoting scores.
- Return ONLY a JSON object, no markdown fences, shaped exactly like this:
{{"headline": "max 12 words", "subhead": "one sentence",
  "lead": ["3 to 5 paragraphs for the front-page story"],
  "recaps": {{"<mid>": {{"headline": "...", "body": "2 to 4 sentences"}}}},
  "column": {{"title": "...", "body": ["2 or 3 opinion paragraphs"]}},
  "storylines": ["3 to 6 short sentences worth remembering in future editions"]}}
Write one recap per game, keyed by that game's "mid"."""
    body = json.dumps({"model": model, "max_tokens": 6000, "system": system,
                       "messages": [{"role": "user", "content": "This week's facts:\n" + json.dumps(facts)}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        return json.loads(text)
    except Exception as e:
        print(f"  Claude didn't return a usable edition ({e}); using the built-in writer instead.")
        return None


def merge_written(base, ai):
    if not isinstance(ai, dict):
        return base
    out = dict(base)
    for k in ("headline", "subhead"):
        if isinstance(ai.get(k), str) and ai[k].strip():
            out[k] = ai[k].strip()
    for k in ("lead", "storylines"):
        if isinstance(ai.get(k), list) and ai[k]:
            out[k] = [str(x) for x in ai[k]]
    if isinstance(ai.get("recaps"), dict):
        rec = dict(base["recaps"])
        for mid, v in ai["recaps"].items():
            if str(mid) in rec and isinstance(v, dict) and v.get("headline") and v.get("body"):
                rec[str(mid)] = {"headline": str(v["headline"]), "body": str(v["body"])}
        out["recaps"] = rec
    c = ai.get("column")
    if isinstance(c, dict) and c.get("title") and isinstance(c.get("body"), list) and c["body"]:
        out["column"] = {"title": str(c["title"]), "body": [str(x) for x in c["body"]]}
    return out


# --------------------------------------------------------------- rendering
CSS = """
:root{--paper:#F1DFD1;--ink:#262626;--head:#000;--rule:#000;--soft:rgba(0,0,0,.28);--win:#1E5B3C;--muted:#5b5048}
*{box-sizing:border-box}
html{background:#d9c6b8}
body{margin:0;color:var(--ink);font:17px/1.55 'Source Serif 4',Georgia,'Times New Roman',serif;font-optical-sizing:auto}
.sheet{max-width:1180px;margin:0 auto;padding:22px 30px 56px;background:var(--paper);min-height:100vh;
  box-shadow:0 0 0 1px rgba(0,0,0,.08),0 18px 50px rgba(60,30,10,.18)}
a{color:inherit}
a:focus-visible{outline:2px solid var(--win);outline-offset:2px}
.ears{display:grid;grid-template-columns:160px 1fr 160px;align-items:center;gap:18px;padding-bottom:8px}
.ear{border:1px solid var(--rule);padding:8px 10px;font-size:13px;line-height:1.35;text-align:center}
.ear b{display:block;font:600 15px/1.2 Oswald,'Arial Narrow',sans-serif}
.title{margin:0;text-align:center;color:var(--head);font:400 clamp(42px,7.6vw,94px)/1.02 'UnifrakturMaguntia','Old English Text MT',serif}
.title a{text-decoration:none}
.dateline{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;border-top:1px solid var(--rule);
  border-bottom:4px double var(--rule);padding:5px 2px;font:500 14px/1.3 Oswald,'Arial Narrow',sans-serif}
.scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));border-bottom:1px solid var(--rule)}
.score{padding:9px 12px;border-right:1px solid var(--soft);font:500 15px/1.4 Oswald,'Arial Narrow',sans-serif;text-decoration:none}
.score:last-child{border-right:0}
.score:hover .tm{text-decoration:underline}
.score div{display:flex;justify-content:space-between;gap:10px}
.score .tm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.score .won{color:var(--win);font-weight:700}
.score .pts{font-variant-numeric:tabular-nums}
.front{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:30px;padding:20px 0 24px;border-bottom:4px double var(--rule)}
.hed{margin:0 0 12px;color:var(--head);font:700 clamp(36px,5.6vw,70px)/.98 Oswald,'Arial Narrow',sans-serif;letter-spacing:-.012em;text-wrap:balance}
.dek{margin:0 0 16px;font-size:21px;line-height:1.35;font-style:italic;max-width:46em}
.cols{column-count:2;column-gap:30px;column-rule:1px solid var(--soft)}
.cols p{margin:0 0 .85em;text-align:justify;hyphens:auto}
.cols p:first-child::first-letter{float:left;font:700 4.1em/.8 Oswald,'Arial Narrow',sans-serif;padding:.06em .09em 0 0;color:var(--head)}
.byline{font:500 13px/1.3 Oswald,'Arial Narrow',sans-serif;color:var(--muted);margin:0 0 12px}
aside.side{border-left:1px solid var(--rule);padding-left:22px}
h3{margin:0 0 10px;padding-top:6px;border-top:4px solid var(--rule);color:var(--head);font:700 22px/1.15 Oswald,'Arial Narrow',sans-serif}
h3 small{font:italic 400 14px 'Source Serif 4',Georgia,serif;color:var(--muted);margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:14.5px;font-variant-numeric:tabular-nums}
th{font:500 13px Oswald,'Arial Narrow',sans-serif;text-align:left;border-bottom:1px solid var(--rule);padding:4px 6px 4px 0}
td{border-bottom:1px solid var(--soft);padding:5px 6px 5px 0;vertical-align:top}
td.n,th.n{text-align:right}
.up{color:var(--win)}
.down{color:#8a2a1c}
.section{padding-top:26px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:26px 30px}
.box h4{margin:0 0 6px;color:var(--head);font:700 23px/1.1 Oswald,'Arial Narrow',sans-serif;text-wrap:balance}
.box p{margin:0 0 10px}
.line{font:500 14px/1.45 Oswald,'Arial Narrow',sans-serif;border-top:1px solid var(--rule);padding-top:6px}
.line div{display:flex;justify-content:space-between}
.perf{font-size:14px;color:var(--muted);margin-top:6px}
.three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:30px}
.three>div+div{border-left:1px solid var(--soft);padding-left:30px}
dl{margin:0}
dt{font:600 16px/1.2 Oswald,'Arial Narrow',sans-serif;color:var(--head);margin-top:12px}
dt:first-child{margin-top:0}
dd{margin:2px 0 0}
.column{background:rgba(255,255,255,.28);border:1px solid var(--rule);padding:16px 20px}
.column h4{margin:0 0 8px;font:italic 600 26px/1.15 'Source Serif 4',Georgia,serif;color:var(--head)}
.column p{margin:0 0 .8em}
ul.plain{margin:0;padding:0;list-style:none}
ul.plain li{padding:6px 0;border-bottom:1px solid var(--soft)}
.previously{font-size:15px}
.previously a{font-weight:600}
footer{margin-top:34px;border-top:4px double var(--rule);padding-top:10px;display:flex;justify-content:space-between;
  gap:12px;flex-wrap:wrap;font:500 15px Oswald,'Arial Narrow',sans-serif}
.archive li{padding:14px 0;border-bottom:1px solid var(--soft);list-style:none}
.archive a{text-decoration:none}
.archive a:hover h4{text-decoration:underline}
.archive h4{margin:2px 0;font:700 28px/1.1 Oswald,'Arial Narrow',sans-serif;color:var(--head)}
.archive p{margin:0;font-style:italic}
@media (max-width:860px){
  .front{grid-template-columns:1fr}
  aside.side{border-left:0;padding-left:0}
  .three{grid-template-columns:1fr}
  .three>div+div{border-left:0;padding-left:0}
}
@media (max-width:620px){
  .sheet{padding:14px 16px 40px}
  .ears{grid-template-columns:1fr}
  .ear{display:none}
  .cols{column-count:1}
}
@media print{html{background:#fff}.sheet{box-shadow:none;background:#fff}}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400;1,8..60,600&family=UnifrakturMaguntia&display=swap" rel="stylesheet">')

e = html.escape


def page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(title)}</title>{FONTS}<style>{CSS}</style></head>'
            f'<body><div class="sheet">{body}</div></body></html>')


def fname(season, week):
    return f"{season}-week-{int(week):02d}.html"


def masthead(paper, left, right, dateline):
    return (f'<header><div class="ears"><div class="ear">{left}</div>'
            f'<h1 class="title"><a href="index.html">{e(paper)}</a></h1><div class="ear">{right}</div></div>'
            f'<div class="dateline">{"".join(f"<span>{e(d)}</span>" for d in dateline)}</div></header>')


def render_edition(ed, vol, prev_key, next_key):
    f, w = ed["facts"], ed["written"]
    aw = f["awards"]
    printed = datetime.fromisoformat(ed["generated_at"]).strftime("%A, %B %-d, %Y") if os.name != "nt" \
        else datetime.fromisoformat(ed["generated_at"]).strftime("%A, %B %#d, %Y")
    n_moves = len(f["moves"]["trades"]) + len(f["moves"]["adds"])
    left = f"<b>Week {f['week']}</b>{'Playoff edition' if f['playoffs'] else 'Regular season'}"
    right = (f"<b>{len(f['games'])} games</b>{n_moves} roster move{'s' if n_moves != 1 else ''}")
    out = [masthead(f["paper"], left, right,
                    [f"Vol. {roman(vol)}, No. {f['week']}", f"{f['league']}, {f['season']} season", printed])]

    # Scoreboard strip
    out.append('<nav class="scores" aria-label="Scores">')
    for g in f["games"]:
        wcls = "" if g["tie"] else " won"
        out.append(f'<a class="score" href="#g{g["mid"]}"><div class="{wcls.strip()}"><span class="tm">{e(g["winner"])}</span>'
                   f'<span class="pts">{fmt(g["wp"])}</span></div><div><span class="tm">{e(g["loser"])}</span>'
                   f'<span class="pts">{fmt(g["lp"])}</span></div></a>')
    out.append("</nav>")

    # Front page
    paras = "".join(f"<p>{e(p)}</p>" for p in w["lead"])
    rows = []
    for r in f["standings"]:
        mv = r["move"]
        arrow = f'<span class="up">+{mv}</span>' if mv > 0 else f'<span class="down">{mv}</span>' if mv < 0 else ""
        rec = f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else "")
        rows.append(f"<tr><td class='n'>{r['rank']}</td><td>{e(r['team'])} {arrow}</td><td class='n'>{rec}</td>"
                    f"<td class='n'>{fmt(r['pf'])}</td><td class='n'>{e(r['streak'])}</td></tr>")
    prev_html = ""
    if prev_key and f["previous"]:
        p = f["previous"][0]
        prev_html = (f'<div class="previously" style="margin-top:22px"><h3>Previously</h3>'
                     f'<p><a href="{fname(*prev_key.split("-"))}">{e(p["headline"] or "Last week")}</a></p>'
                     + "".join(f"<p>{e(s)}</p>" for s in p["storylines"][:2]) + "</div>")
    out.append(f'<section class="front"><article><h2 class="hed">{e(w["headline"])}</h2>'
               f'<p class="dek">{e(w["subhead"])}</p><p class="byline">By the editor</p>'
               f'<div class="cols">{paras}</div></article>'
               f'<aside class="side"><h3>Standings</h3><table><thead><tr><th class="n">#</th><th>Team</th>'
               f'<th class="n">W-L</th><th class="n">PF</th><th class="n">Strk</th></tr></thead>'
               f'<tbody>{"".join(rows)}</tbody></table>{prev_html}</aside></section>')

    # Box scores
    out.append('<section class="section"><h3>Around the league <small>Every game, every grudge</small></h3><div class="grid">')
    for g in f["games"]:
        rc = w["recaps"].get(str(g["mid"]), {"headline": "", "body": ""})
        perf = "; ".join(f"{t['name']} {fmt(t['pts'])}" for t in g["winner_top"][:2])
        perf_l = "; ".join(f"{t['name']} {fmt(t['pts'])}" for t in g["loser_top"][:2])
        tag = f"<p class='perf'><i>{e(g['rivalry'])}</i></p>" if g["rivalry"] else ""
        out.append(f'<article class="box" id="g{g["mid"]}"><h4>{e(rc["headline"])}</h4>{tag}<p>{e(rc["body"])}</p>'
                   f'<div class="line"><div><span class="{"" if g["tie"] else "up"}">{e(g["winner"])}</span><span>{fmt(g["wp"])}</span></div>'
                   f'<div><span>{e(g["loser"])}</span><span>{fmt(g["lp"])}</span></div></div>'
                   f'<p class="perf">{e(perf)}<br>{e(perf_l)}</p></article>')
    out.append("</div></section>")

    # Awards, column, rivalries
    dl = []
    if aw.get("player_of_week"):
        p = aw["player_of_week"]
        dl.append(("Player of the week", f"{p['player']} ({p['pos']}), {fmt(p['pts'])} for {p['team']}"))
    if aw.get("high"):
        dl.append(("High score", f"{aw['high']['team']}, {fmt(aw['high']['pts'])}"))
    if aw.get("low"):
        dl.append(("Low score", f"{aw['low']['team']}, {fmt(aw['low']['pts'])}"))
    if aw.get("bench_blunder"):
        b = aw["bench_blunder"]
        verdict = " It cost them the game." if b["cost_game"] else ""
        dl.append(("Bench blunder", f"{b['team']} sat {b['bench']} ({fmt(b['bench_pts'])}) and started "
                                    f"{b['starter']} ({fmt(b['starter_pts'])}).{verdict}"))
    if aw.get("dud"):
        d = aw["dud"]
        dl.append(("Started and regretted", f"{d['player']} ({d['pos']}), {fmt(d['pts'])} for {d['team']}"))
    if f["waiver_wonder"]:
        p = f["waiver_wonder"]
        dl.append(("Waiver wonder", f"{p['player']} gave {p['team']} {fmt(p['this_week'])} after a week-{p['week_added']} pickup"))
    for r in f["records"]:
        dl.append(("For the record", r))
    if f["one_year_ago"]:
        dl.append(("One year ago", f["one_year_ago"]))
    awards_html = "<dl>" + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in dl) + "</dl>"
    col = w["column"]
    col_html = (f'<div class="column"><h4>{e(col["title"])}</h4>' + "".join(f"<p>{e(p)}</p>" for p in col["body"]) + "</div>")
    riv = []
    for r in f["rivalry_watch"]:
        title = f"<i>{e(r['title'])}</i><br>" if r["title"] else ""
        state = f"{e(r['leader'])} leads {r['record']}" if r["leader"] else f"All square at {r['record']}"
        riv.append(f"<li>{title}<b>{e(r['teams'][0])}</b> vs <b>{e(r['teams'][1])}</b><br>"
                   f"{state} across {r['meetings']} meetings, average margin {fmt(r['avg_margin'])}</li>")
    riv_html = f'<ul class="plain">{"".join(riv)}</ul>' if riv else "<p>No series has history yet. Give it time.</p>"
    streak_html = "".join(f"<p>{e(s)}</p>" for s in f["streaks"])
    if streak_html:
        streak_html = '<div style="margin-top:20px"><h3>Streak watch</h3>' + streak_html + "</div>"
    out.append(f'<section class="section three"><div><h3>The notebook</h3>{awards_html}</div>'
               f'<div>{col_html}{streak_html}</div>'
               f'<div><h3>Rivalry watch</h3>{riv_html}</div></section>')

    # Transactions
    tw = []
    for t in f["moves"]["trades"]:
        tw.append("<li><b>Trade.</b> " + e("; ".join(f"{s['team']} gets {', '.join(s['got']) or 'nothing'}" for s in t["sides"])) + "</li>")
    for a in f["moves"]["adds"]:
        bid = f" for ${a['bid']}" if a.get("bid") else ""
        drop = f", dropping {', '.join(a['dropped'])}" if a["dropped"] else ""
        tw.append(f"<li><b>{e(a['team'])}</b> added {e(a['player'])} ({e(a['pos'])}) via {e(a['via'])}{e(bid)}{e(drop)}</li>")
    tw_html = f'<ul class="plain">{"".join(tw)}</ul>' if tw else "<p>No moves this week.</p>"

    tt = []
    for t in f["trade_tracker"]:
        deal = "<br>".join(f"<b>{e(s['team'])}</b> got {e(', '.join(s['got']) or 'nothing')}: {fmt(s['pts'])} pts" for s in t["sides"])
        edge = f"{e(t['leader'])} by {fmt(t['gap'])}" if t["leader"] else "Even"
        tt.append(f"<tr><td class='n'>{t['week']}</td><td>{deal}</td><td>{edge}</td></tr>")
    tt_html = (f"<table><thead><tr><th class='n'>Wk</th><th>Deal and points started since</th><th>Edge</th></tr></thead>"
               f"<tbody>{''.join(tt)}</tbody></table>") if tt else "<p>No trades yet this season. Somebody pick up the phone.</p>"

    hof = [f"<tr><td>{e(p['player'])} <span class='perf'>{e(p['pos'])}</span></td><td>{e(p['team'])}</td>"
           f"<td class='n'>{p['week_added']}</td><td class='n'>{p['starts']}</td><td class='n'>{fmt(p['pts'])}</td></tr>"
           for p in f["pickup_hof"]]
    hof_html = (f"<table><thead><tr><th>Player</th><th>Team</th><th class='n'>Added</th><th class='n'>Starts</th>"
                f"<th class='n'>Pts</th></tr></thead><tbody>{''.join(hof)}</tbody></table>") if hof else "<p>No pickup has paid off yet.</p>"
    out.append(f'<section class="section three"><div><h3>Transaction wire</h3>{tw_html}</div>'
               f'<div><h3>Trade tracker <small>Who won the deal?</small></h3>{tt_html}</div>'
               f'<div><h3>Best pickups of {e(f["season"])}</h3>{hof_html}</div></section>')

    nav_prev = f'<a href="{fname(*prev_key.split("-"))}">Previous edition</a>' if prev_key else "<span></span>"
    nav_next = f'<a href="{fname(*next_key.split("-"))}">Next edition</a>' if next_key else "<span></span>"
    out.append(f'<footer>{nav_prev}<a href="index.html">All editions</a>{nav_next}</footer>')
    return page(f"{f['paper']}: week {f['week']}, {f['season']}", "".join(out))


def render_all(arc, out_dir, paper):
    eds = sorted(arc.get("editions", {}), key=lambda k: gkey(*k.split("-")))
    seasons = sorted(arc["seasons"], key=int)
    for i, k in enumerate(eds):
        ed = arc["editions"][k]
        vol = seasons.index(ed["season"]) + 1 if ed["season"] in seasons else 1
        prev_key = eds[i - 1] if i > 0 else None
        next_key = eds[i + 1] if i + 1 < len(eds) else None
        (out_dir / fname(ed["season"], ed["week"])).write_text(render_edition(ed, vol, prev_key, next_key), encoding="utf-8")
    items = []
    for k in reversed(eds):
        ed = arc["editions"][k]
        items.append(f'<li><a href="{fname(ed["season"], ed["week"])}"><span class="byline">Week {ed["week"]}, {e(ed["season"])}</span>'
                     f'<h4>{e(ed["written"]["headline"])}</h4><p>{e(ed["written"]["subhead"])}</p></a></li>')
    body = masthead(paper, "<b>The archive</b>Every edition", f"<b>{len(eds)}</b>editions printed",
                    ["All editions", "Newest first", ""])
    body += f'<ul class="archive" style="padding:0;margin:0">{"".join(items)}</ul>'
    (out_dir / "index.html").write_text(page(f"{paper}: archive", body), encoding="utf-8")


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Print this week's league newspaper from Sleeper.")
    ap.add_argument("--league", default=os.environ.get("SLEEPER_LEAGUE_ID") or LEAGUE_ID, help="Sleeper league ID")
    ap.add_argument("--week", type=int, help="Week to print (default: the most recent week with scores)")
    ap.add_argument("--out", default="gazette", help="Output folder (default: ./gazette)")
    ap.add_argument("--history", type=int, default=5, help="Prior seasons to backfill for rivalries (default 5)")
    ap.add_argument("--no-ai", action="store_true", help="Use the built-in writer even if ANTHROPIC_API_KEY is set")
    args = ap.parse_args()
    if not args.league:
        sys.exit("Add your league ID: python sleeper_gazette.py --league 1234567890 (or set LEAGUE_ID at the top).")

    out_dir = Path(args.out)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    arc_path = data_dir / f"archive-{args.league}.json"
    arc = json.loads(arc_path.read_text(encoding="utf-8")) if arc_path.exists() else {"version": 1, "seasons": {}, "editions": {}}

    lg = get(f"/league/{args.league}")
    if not lg:
        sys.exit(f"Sleeper didn't recognize league {args.league}. Check the ID in your league URL.")
    season = str(lg["season"])

    week = args.week
    if week is None:
        if lg.get("status") == "complete":
            week = None  # resolved after sync
        else:
            state = get("/state/nfl") or {}
            week = int(state.get("week") or 1) if str(state.get("season")) == season else 18
            ms = get(f"/league/{args.league}/matchups/{week}") or []
            while week > 1 and not has_points(ms):
                week -= 1
                ms = get(f"/league/{args.league}/matchups/{week}") or []

    print(f"Syncing {lg.get('name')} ({season})...")
    sync_league(arc, args.league, through_week=week, force=tuple(w for w in (week, (week or 1) - 1) if w))
    if week is None:
        week = max((int(k) for k in arc["seasons"][season]["weeks"]), default=1)

    prev_id, depth = lg.get("previous_league_id"), 0
    while prev_id and prev_id != "0" and depth < args.history:
        print("Backfilling an earlier season...")
        plg = sync_league(arc, prev_id)
        prev_id, depth = plg.get("previous_league_id"), depth + 1

    if str(week) not in arc["seasons"][season]["weeks"]:
        sys.exit(f"No scores for week {week} yet. Try again after the games, or pass --week.")

    load_players(data_dir)
    paper = PAPER_NAME or f"The {arc['seasons'][season]['name']} Gazette"
    facts = compute_facts(arc, season, week, paper)
    written = template_write(facts)
    key = os.environ.get("ANTHROPIC_API_KEY")
    used_ai = False
    if key and not args.no_ai:
        print("Claude is writing this week's edition...")
        ai = ai_write(facts, MODEL, key)
        used_ai = ai is not None
        written = merge_written(written, ai)

    arc.setdefault("editions", {})[f"{season}-{week}"] = {
        "season": season, "week": week, "facts": facts, "written": written,
        "generated_at": datetime.now().isoformat(timespec="seconds"), "ai": used_ai}
    arc_path.write_text(json.dumps(arc), encoding="utf-8")
    render_all(arc, out_dir, paper)
    print(f"Done: {(out_dir / fname(season, week)).resolve()}")


if __name__ == "__main__":
    main()
