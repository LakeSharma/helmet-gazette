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
import math
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
# What to call each manager in articles. Match on team name or Sleeper display name
# (capitals, spaces and punctuation are ignored). Anyone not listed keeps their display name.
MANAGER_NAMES = {
    "GopherDeeznuts": "Jorguss",
    "madmaxwellog49": "Max",
    "They Laported the Nabers": "Lake",
    "Boomer Sooner": "Silverhammer",
    "RomoCop": "Connor",
    "THE crab warriors": "Jack",
    "Olichode": "Oliver",
    "Love is War": "Mike",
    "Bower Rangers": "Jared",
    "AMBULANCE!": "Chris",
    "Jared's Big Papa": "Austin",
    "Let Him Cook": "Austin Mick",
    "mikhawk": "Jackson",
    "mikehawk": "Jackson",
}
SHOW_PHOTOS = False  # Set to True to bring back the illustrated player photos
MODEL = os.environ.get("GAZETTE_MODEL", "claude-sonnet-5-5")
# --------------------------------------------------------------------------

SLEEPER = "https://api.sleeper.app/v1"
PLAYERS = {}
STATS = {}   # season -> {"stats": {week: {pid: {...}}}, "proj": {week: {pid: pts}}}


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", str(x or "").lower())


_MGR = {_norm(k): v for k, v in MANAGER_NAMES.items()}


def real_name(owner):
    """A manager's name for articles: MANAGER_NAMES match on team or display name, else display name."""
    return _MGR.get(_norm(owner.get("team"))) or _MGR.get(_norm(owner.get("name"))) or owner.get("name") or "Unknown"


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
    path = data_dir / "players-v3.json"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < 20 * 3600
    if not fresh:
        print("Downloading the Sleeper player list (about 5 MB, refreshed daily for injury news)...")
        try:
            raw = get("/players/nfl") or {}
            slim = {}
            for pid, p in raw.items():
                name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                slim[pid] = [name or pid, p.get("position") or "", p.get("team") or "", p.get("espn_id") or "",
                             p.get("injury_status") or "", p.get("injury_body_part") or "",
                             p.get("years_exp"), p.get("age")]
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


def photo(pid):
    """Headshot URL plus a backup URL, or team logos for defenses."""
    pid = str(pid)
    if pid.isalpha():
        t = pid.lower()
        return (f"https://sleepercdn.com/images/team_logos/nfl/{t}.png",
                f"https://a.espncdn.com/i/teamlogos/nfl/500/{t}.png")
    p = PLAYERS.get(pid)
    espn = p[3] if p and len(p) > 3 and p[3] else ""
    sleeper = f"https://sleepercdn.com/content/nfl/players/{pid}.jpg"
    if espn:
        return (f"https://a.espncdn.com/i/headshots/nfl/players/full/{espn}.png", sleeper)
    return (sleeper, "")


def pinfo(pid, i, default=None):
    p = PLAYERS.get(str(pid))
    return p[i] if p and len(p) > i else default


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
    st = lg.get("settings") or {}
    s.update(league_id=league_id, name=lg.get("name") or s.get("name") or "Our League",
             playoff_week_start=st.get("playoff_week_start") or 0,
             playoff_teams=st.get("playoff_teams") or 0,
             roster_positions=lg.get("roster_positions") or [],
             scoring=lg.get("scoring_settings") or s.get("scoring") or {})
    if lg.get("status") == "complete" and "champion" not in s:
        s["champion"] = champion_of(league_id)
    if s.get("complete") and through_week is None:
        return lg
    users = get(f"/league/{league_id}/users") or []
    rosters = get(f"/league/{league_id}/rosters") or []
    s["owners"] = {u["user_id"]: {"name": u.get("display_name") or "Unknown",
                                  "team": (u.get("metadata") or {}).get("team_name") or u.get("display_name") or "Unknown",
                                  "avatar": u.get("avatar")}
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
    # Upcoming regular-season pairings, for previews and playoff odds
    pws = s.get("playoff_week_start") or 0
    if through_week and pws:
        sched = s.setdefault("schedule", {})
        for w in range(through_week + 1, pws):
            if str(w) in sched:
                continue
            groups = defaultdict(list)
            for m in get(f"/league/{league_id}/matchups/{w}") or []:
                if m.get("matchup_id") is not None:
                    groups[m["matchup_id"]].append(m["roster_id"])
            pairs = [g for g in groups.values() if len(g) == 2]
            if pairs:
                sched[str(w)] = pairs
    if lg.get("status") == "complete":
        s["complete"] = True
    return lg


def champion_of(league_id):
    for m in get(f"/league/{league_id}/winners_bracket") or []:
        if m.get("p") == 1 and m.get("w"):
            return {"winner": m["w"], "runner_up": m.get("l")}
    return None


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
            best = {"bench": pname(bp), "bench_pid": bp, "bench_pos": pos, "bench_pts": m["pp"].get(bp, 0),
                    "starter": pname(worst), "starter_pts": m["pp"].get(worst, 0), "gain": round(gain, 2)}
    return best


def top_starters(m, n=3):
    ranked = sorted(m["starters"], key=lambda p: -m["pp"].get(p, 0))[:n]
    return [{"pid": p, "name": pname(p), "pos": ppos(p), "nfl": pteam(p), "pts": m["pp"].get(p, 0)} for p in ranked]


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
                return real_name(o)
        return "Unknown"

    def avatar(oid):
        a = s["owners"].get(oid, {}).get("avatar")
        return f"https://sleepercdn.com/avatars/thumbs/{a}" if a else None

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
            awards["player_of_week"] = {"pid": p, "player": pname(p), "pos": ppos(p), "team": team(oid), "pts": pts}
            skill = [x for x in starters if ppos(x[1]) in ("QB", "RB", "WR", "TE")]
            if skill:
                oid, p, pts = min(skill, key=lambda x: x[2])
                awards["dud"] = {"pid": p, "player": pname(p), "pos": ppos(p), "team": team(oid), "pts": pts}
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
        hi_pts, hi_oid = awards["high"]["pts"], hi[0]
        lo_oid = lo[0]
        prev_scores = [p for g in allg if gkey(g["season"], g["week"]) < now for p in (g["pa"], g["pb"])]
        season_prev = [p for g in allg if g["season"] == season and g["week"] < week for p in (g["pa"], g["pb"])]
        if prev_scores and hi_pts > max(prev_scores):
            records.append(f"{manager(hi_oid)} set the all-time league record with {fmt(hi_pts)} points (old mark {fmt(max(prev_scores))}).")
        elif season_prev and hi_pts > max(season_prev):
            records.append(f"{manager(hi_oid)}'s {fmt(hi_pts)} is the highest score of the {season} season.")
        lo_pts = awards["low"]["pts"]
        if prev_scores and lo_pts < min(prev_scores):
            records.append(f"{manager(lo_oid)}'s {fmt(lo_pts)} is the lowest score in league history.")

    # Standings with movement
    stand = []
    for r in st:
        mv = prev_st.get(r["oid"], r["rank"]) - r["rank"] if prev_st else 0
        stand.append({"rank": r["rank"], "team": team(r["oid"]), "manager": manager(r["oid"]), "avatar": avatar(r["oid"]),
                      "w": r["w"], "l": r["l"], "t": r["t"], "pf": r["pf"], "pa": r["pa"],
                      "streak": r["streak"], "move": mv})
    streaks = []
    for r in st:
        n = int(r["streak"][1:] or 0) if r["streak"] else 0
        if n >= 3 and r["streak"][0] in "WL":
            word = "won" if r["streak"][0] == "W" else "lost"
            streaks.append(f"{manager(r['oid'])} has {word} {n} straight.")
    if week >= 3:
        for r in st:
            if r["l"] == 0 and r["w"] == week:
                streaks.append(f"{manager(r['oid'])} is still unbeaten at {r['w']}-0.")
            if r["w"] == 0 and r["l"] == week:
                streaks.append(f"{manager(r['oid'])} is still looking for his first win (0-{r['l']}).")

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
                    pickups.append({"pid": p, "player": pname(p), "pos": ppos(p), "team": team(owner_of(s, rid)),
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

    # Player profiles for everyone the stories might mention
    latest = week >= max(int(k) for k in s["weeks"])
    ranks = position_ranks(season, week)
    week_pp = {}
    for m in s["weeks"][str(week)]["matchups"]:
        week_pp.update(m["pp"])
    profiles = {}
    def prof(pid):
        if pid and pid not in profiles:
            profiles[pid] = dossier(arc, season, week, pid, week_pp.get(pid), ranks, latest)
        return profiles.get(pid) or {}
    for gf in gfacts:
        for t in gf["winner_top"][:2] + gf["loser_top"][:2]:
            prof(t["pid"])
        for b in gf["blunders"]:
            prof(b.get("bench_pid"))
    for k in ("player_of_week", "dud"):
        if awards.get(k):
            prof(awards[k]["pid"])
    if wonder:
        prof(wonder["pid"])

    # Story angles for each game, as hints for the writer
    streak_of_oid = {r["oid"]: r["streak"] for r in st}
    proj_week = (STATS.get(season) or {}).get("proj", {}).get(str(week), {})
    for g, gf in zip(games, gfacts):
        mW, mL = (g["ma"], g["mb"]) if gf["winner_oid"] == g["a"] else (g["mb"], g["ma"])
        angles = []
        for side, m, mgr in (("winner", mW, gf["winner_manager"]), ("loser", mL, gf["loser_manager"])):
            for pid in m["starters"]:
                pts, d = m["pp"].get(pid, 0), None
                proj = proj_week.get(pid)
                if proj and proj >= 14 and pts <= 0.4 * proj:
                    prof(pid)
                    angles.append({"type": "bust", "player": pname(pid), "manager": mgr, "side": side,
                                   "detail": f"projected {fmt(proj)}, scored {fmt(pts)}"})
            tops = gf["winner_top"] if side == "winner" else gf["loser_top"]
            for t in tops[:2]:
                d = profiles.get(t["pid"]) or {}
                notes = " ".join(d.get("notes", []))
                base = {"player": t["name"], "manager": mgr, "side": side}
                if "Returned this week" in notes:
                    angles.append(dict(base, type="comeback"))
                if t["pts"] >= 35:
                    angles.append(dict(base, type="nuke"))
                elif "Season high" in notes and t["pts"] >= 20:
                    angles.append(dict(base, type="breakout"))
                if d.get("rookie") and t["pts"] >= 18:
                    angles.append(dict(base, type="rookie"))
        if any(b.get("cost_game") for b in gf["blunders"]):
            angles.append({"type": "bench_blunder", "manager": gf["loser_manager"]})
        if gf["upset"]:
            angles.append({"type": "upset"})
        if gf["rivalry"]:
            angles.append({"type": "rivalry", "name": gf["rivalry"]})
        lm = gf.get("last_meeting")
        if lm and not gf["tie"] and lm["winner"] == gf["loser"]:
            angles.append({"type": "revenge", "manager": gf["winner_manager"]})
        for oid, mgr in ((gf["winner_oid"], gf["winner_manager"]), (gf["loser_oid"], gf["loser_manager"])):
            sk = streak_of_oid.get(oid, "")
            if sk and int(sk[1:] or 0) >= 3:
                angles.append({"type": "streak", "manager": mgr, "streak": sk})
        if gf["margin"] >= 40:
            angles.append({"type": "blowout"})
        elif gf["margin"] < 3 and not gf["tie"]:
            angles.append({"type": "nailbiter"})
        gf["angles"] = angles

    # Players the paper could put in a photo, for the writer to choose from
    cands = {}
    def cand(pid, name, why, mid=None):
        if pid and name and pid not in cands:
            cands[pid] = {"player": name, "why": why, "mid": mid}
    if awards.get("player_of_week"):
        cand(awards["player_of_week"]["pid"], awards["player_of_week"]["player"], "player of the week")
    for gf in gfacts:
        for t in gf["winner_top"][:2]:
            cand(t["pid"], t["name"], f"top scorer for {gf['winner_manager']}", gf["mid"])
        for t in gf["loser_top"][:1]:
            cand(t["pid"], t["name"], f"top scorer for {gf['loser_manager']} in a loss", gf["mid"])
        for b in gf["blunders"]:
            cand(b.get("bench_pid"), b["bench"], f"left on the bench by {gf['winner_manager'] if b['team'] == gf['winner'] else gf['loser_manager']}", gf["mid"])
    if awards.get("bench_blunder"):
        b = awards["bench_blunder"]
        cand(b.get("bench_pid"), b["bench"], f"left on {b['team']}'s bench")
    if awards.get("dud"):
        cand(awards["dud"]["pid"], awards["dud"]["player"], "worst starter of the week")
    if wonder:
        cand(wonder["pid"], wonder["player"], "waiver pickup who delivered")
    photo_candidates = [{"pid": k, **v} for k, v in cands.items()]
    managers = {team(o): manager(o) for o in set(s["roster_owner"].values())}

    return {"paper": paper, "league": s["name"], "season": season, "week": week,
            "playoffs": is_playoffs(s, week), "games": gfacts, "standings": stand,
            "awards": awards, "records": records, "streaks": streaks, "moves": moves,
            "trade_tracker": trade_tracker, "pickup_hof": hof, "waiver_wonder": wonder,
            "rivalry_watch": rivalry_watch, "one_year_ago": one_year_ago, "previous": previous, "photo_candidates": photo_candidates,
            "players": profiles, "managers": managers,
            **extra_facts(arc, season, week, team, named_rivals, st)}



# ---------------------------------------------------------- player history
STAT_KEYS = ["pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd", "rush_td", "rec", "rec_tgt", "rec_yd",
             "rec_td", "fum_lost", "fgm", "fga", "fgm_lng", "xpm", "def_td", "int", "sack", "pts_allow"]
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]


def _rows(data):
    """Sleeper returns stats either as {pid: stats} or as [{player_id, stats}]."""
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    if isinstance(data, list):
        return {str(r.get("player_id")): (r.get("stats") or {}) for r in data if isinstance(r, dict) and r.get("player_id")}
    return {}


def fantasy_pts(st, scoring):
    if scoring:
        return round(sum(float(v) * scoring[k] for k, v in st.items()
                         if k in scoring and isinstance(v, (int, float)) and isinstance(scoring[k], (int, float))), 2)
    return r2(st.get("pts_ppr"))


def fetch_week(kind, season, w):
    pos = "&".join(f"position[]={p}" for p in POSITIONS)
    for url in (f"/{kind}/nfl/regular/{season}/{w}",
                f"https://api.sleeper.app/{kind}/nfl/{season}/{w}?season_type=regular&{pos}"):
        try:
            rows = _rows(get(url))
        except Exception:
            rows = {}
        if rows:
            return rows
    return {}


def sync_stats(s, season, through, data_dir):
    """Every NFL player's weekly stats and projections for the season, cached in data/stats-<season>.json."""
    path = data_dir / f"stats-{season}.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        cache = {}
    cache.setdefault("stats", {})
    cache.setdefault("proj", {})
    scoring = s.get("scoring") or {}
    changed = False
    for w in range(1, through + 2):
        k = str(w)
        if w <= through and (k not in cache["stats"] or w >= through - 1):
            rows = fetch_week("stats", season, w)
            if rows:
                slim = {}
                for pid, st in rows.items():
                    pts = fantasy_pts(st, scoring)
                    gp = st.get("gp") or (1 if pts or any(st.get(x) for x in STAT_KEYS) else 0)
                    if not gp:
                        continue
                    d = {x: st[x] for x in STAT_KEYS if st.get(x)}
                    if pid.isalpha() and "pts_allow" in st:
                        d["pts_allow"] = st["pts_allow"]
                    d["pts"], d["gp"] = pts, 1
                    slim[pid] = d
                cache["stats"][k] = slim
                changed = True
        if k not in cache["proj"] or w >= through:
            rows = fetch_week("projections", season, w)
            if rows:
                cache["proj"][k] = {pid: v for pid, v in ((pid, fantasy_pts(st, scoring)) for pid, st in rows.items()) if v >= 1}
                changed = True
    if changed:
        path.write_text(json.dumps(cache), encoding="utf-8")
    if not cache["stats"]:
        print("  Couldn't get NFL player stats from Sleeper; articles will use league scoring only.")
    STATS[season] = cache


def _n(x):
    return f"{float(x):g}"


def stat_line(d, pos):
    if not d:
        return ""
    parts = []
    if pos == "DEF":
        if d.get("sack"):
            parts.append(f"{_n(d['sack'])} sacks")
        if d.get("int"):
            parts.append(f"{_n(d['int'])} interceptions")
        if d.get("def_td"):
            parts.append(f"{_n(d['def_td'])} defensive TD")
        if "pts_allow" in d:
            parts.append(f"{_n(d['pts_allow'])} points allowed")
        return ", ".join(parts)
    if pos == "K":
        if d.get("fga"):
            parts.append(f"{_n(d.get('fgm', 0))} of {_n(d['fga'])} field goals" + (f" (long {_n(d['fgm_lng'])})" if d.get("fgm_lng") else ""))
        if d.get("xpm"):
            parts.append(f"{_n(d['xpm'])} extra points")
        return ", ".join(parts)
    if d.get("pass_yd"):
        parts.append(f"{_n(d['pass_yd'])} passing yards" + (f", {_n(d['pass_td'])} TD" if d.get("pass_td") else "")
                     + (f", {_n(d['pass_int'])} INT" if d.get("pass_int") else ""))
    if d.get("rush_yd") or d.get("rush_td"):
        parts.append(f"{_n(d.get('rush_att', 0))} carries for {_n(d.get('rush_yd', 0))} yards"
                     + (f" and {_n(d['rush_td'])} TD" if d.get("rush_td") else ""))
    if d.get("rec"):
        parts.append(f"{_n(d['rec'])} catches" + (f" on {_n(d['rec_tgt'])} targets" if d.get("rec_tgt") else "")
                     + f" for {_n(d.get('rec_yd', 0))} yards" + (f" and {_n(d['rec_td'])} TD" if d.get("rec_td") else ""))
    if d.get("fum_lost"):
        parts.append(f"{_n(d['fum_lost'])} lost fumble")
    return "; ".join(parts)


def position_ranks(season, week):
    c = STATS.get(season) or {}
    st = c.get("stats", {})
    total = defaultdict(float)
    for w in range(1, week + 1):
        for pid, d in st.get(str(w), {}).items():
            total[pid] += d.get("pts", 0)
    def rank(scores):
        by = defaultdict(list)
        for pid, v in scores.items():
            if ppos(pid) in POSITIONS:
                by[ppos(pid)].append((v, pid))
        out = {}
        for pos, lst in by.items():
            lst.sort(reverse=True)
            for i, (_, pid) in enumerate(lst, 1):
                out[pid] = f"{pos}{i}"
        return out
    return rank({p: d.get("pts", 0) for p, d in st.get(str(week), {}).items()}), rank(total)


def dossier(arc, season, week, pid, league_pts=None, ranks=None, latest=False):
    """Everything worth knowing about one player's week, as plain-English notes."""
    c = STATS.get(season) or {"stats": {}, "proj": {}}
    pos, nfl = ppos(pid), pteam(pid)
    log = []
    for w in range(1, week + 1):
        wk = c["stats"].get(str(w))
        if not wk:
            continue
        d = wk.get(pid)
        teams_played = {k for k in wk if k.isalpha()}
        if d:
            log.append({"week": w, "pts": d["pts"]})
        elif nfl and teams_played:
            log.append({"week": w, "status": "did not play" if nfl in teams_played else "bye"})
    this = c["stats"].get(str(week), {}).get(pid)
    pts = league_pts if league_pts is not None else (this or {}).get("pts")
    proj = c["proj"].get(str(week), {}).get(pid)
    notes = []
    before = [x for x in log if x["week"] < week]
    missed = 0
    for x in reversed(before):
        if x.get("status") == "bye":
            continue
        if x.get("status") == "did not play":
            missed += 1
        else:
            break
    inj_hist = [(k, v) for k, v in (arc.get("injury_log", {}).get(pid) or {}).items()
                if k.split("-")[0] == season and int(k.split("-")[1]) <= week]
    body = next((v[1] for k, v in reversed(inj_hist) if v[1]), "")
    if this and missed:
        notes.append(f"Returned this week after missing {missed} game{'s' if missed > 1 else ''}"
                     + (f" ({body})" if body else "") + ".")
    if not this and log and log[-1]["week"] == week and log[-1].get("status") == "did not play":
        notes.append("Did not play this week" + (f" ({body})" if body else "") + ".")
    if log and log[-1]["week"] == week and log[-1].get("status") == "bye":
        notes.append("Was on bye this week.")
    played = [x["pts"] for x in before if "pts" in x]
    if this and played:
        avg = sum(played) / len(played)
        if this["pts"] > max(played) and len(played) >= 2:
            notes.append(f"Season high (previous best {fmt(max(played))}).")
        elif this["pts"] < min(played) and len(played) >= 2:
            notes.append(f"Season low (previous worst {fmt(min(played))}).")
        if this["pts"] - avg >= 10:
            notes.append(f"Well above his {fmt(avg)} average coming in.")
        elif avg - this["pts"] >= 10:
            notes.append(f"Well below his {fmt(avg)} average coming in.")
        last = [x["pts"] for x in log if "pts" in x][-3:]
        if len(last) == 3 and pos in ("QB", "RB", "WR", "TE"):
            if all(v < 8 for v in last):
                notes.append("Under 8 fantasy points in three straight games.")
            elif all(v >= 20 for v in last):
                notes.append("20-plus fantasy points in three straight games.")
    if this and proj:
        diff = this["pts"] - proj
        if abs(diff) >= 8:
            notes.append(f"Projected {fmt(proj)}, so he {'beat' if diff > 0 else 'missed'} it by {fmt(abs(diff))}.")
    if pinfo(pid, 6) == 0:
        notes.append("Rookie.")
    age = pinfo(pid, 7)
    if age and age >= 33:
        notes.append(f"Age {age}.")
    if ranks and this:
        wr, sr = ranks[0].get(pid), ranks[1].get(pid)
        if wr:
            notes.append(f"{wr} this week" + (f", {sr} on the season" if sr else "") + ".")
    runs = []
    for k, v in sorted(inj_hist, key=lambda kv: int(kv[0].split("-")[1])):
        wk_ = int(k.split("-")[1])
        if wk_ >= week:
            continue
        if runs and runs[-1][2] == tuple(v) and runs[-1][1] == wk_ - 1:
            runs[-1][1] = wk_
        else:
            runs.append([wk_, wk_, tuple(v)])
    for a_, b_, v in runs:
        when = f"week {a_}" if a_ == b_ else f"weeks {a_}-{b_}"
        notes.append(f"Was listed {v[0]}" + (f" ({v[1]})" if v[1] else "") + f" in {when}.")
    if latest and pinfo(pid, 4):
        notes.append(f"Current injury designation: {pinfo(pid, 4)}" + (f" ({pinfo(pid, 5)})" if pinfo(pid, 5) else "") + ".")
    games = [x for x in log if "pts" in x]
    return {"name": pname(pid), "pos": pos, "nfl": nfl, "pts": pts, "projection": proj,
            "stat_line": stat_line(this, pos), "rookie": pinfo(pid, 6) == 0,
            "season_avg": round(sum(x["pts"] for x in games) / len(games), 2) if games else None,
            "recent_games": log[-5:], "notes": notes}


def record_injuries(arc, s, season, week):
    """Save this week's injury designations so future editions know who was hurt and when."""
    log = arc.setdefault("injury_log", {})
    wk = s["weeks"].get(str(week), {})
    for m in wk.get("matchups", []):
        for pid in m["players"]:
            st = pinfo(pid, 4)
            if st:
                log.setdefault(pid, {})[f"{season}-{week}"] = [st, pinfo(pid, 5) or ""]


# ------------------------------------------------------------ deeper stats
FLEX_SLOTS = {"FLEX": {"RB", "WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"},
              "WRTE_FLEX": {"WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "IDP_FLEX": {"DL", "LB", "DB"}}
BENCH_SLOTS = {"BN", "IR", "TAXI"}


def optimal_points(m, slots):
    """Best possible lineup from this roster, filling the strictest slots first."""
    slots = [x for x in slots if x not in BENCH_SLOTS]
    if not slots:
        return None
    pool = sorted(m["players"], key=lambda p: -m["pp"].get(p, 0))
    used, total = set(), 0.0
    for slot in sorted(slots, key=lambda x: len(FLEX_SLOTS.get(x, {x}))):
        ok = FLEX_SLOTS.get(slot, {slot})
        for p in pool:
            if p not in used and ppos(p) in ok:
                used.add(p)
                total += m["pp"].get(p, 0)
                break
    return round(max(total, m["points"]), 2)


def pairings(s, w):
    wk = s["weeks"].get(str(w))
    if wk:
        groups = defaultdict(list)
        for m in wk["matchups"]:
            if m.get("matchup_id") is not None:
                groups[m["matchup_id"]].append(m["roster_id"])
        pairs = [g for g in groups.values() if len(g) == 2]
    else:
        pairs = s.get("schedule", {}).get(str(w), [])
    return [(owner_of(s, a), owner_of(s, b)) for a, b in pairs]


def score_log(s, through):
    """Regular-season scores per owner: {oid: [(week, pts), ...]}"""
    log = defaultdict(list)
    for w in range(1, through + 1):
        if is_playoffs(s, w) or str(w) not in s["weeks"]:
            continue
        for m in s["weeks"][str(w)]["matchups"]:
            if m.get("matchup_id") is not None:
                log[owner_of(s, m["roster_id"])].append((w, m["points"]))
    return log


def score_model(log):
    pts = [p for v in log.values() for _, p in v]
    if len(pts) < 2:
        return None
    mean = sum(pts) / len(pts)
    resid = []
    for v in log.values():
        if len(v) >= 2:
            mu = sum(p for _, p in v) / len(v)
            resid += [p - mu for _, p in v]
    sd = (sum(r * r for r in resid) / len(resid)) ** 0.5 if len(resid) > 3 else 25.0
    means = {o: (sum(p for _, p in v) + 2 * mean) / (len(v) + 2) for o, v in log.items()}  # shrink toward average
    return {"mean": mean, "sd": max(sd, 12.0), "means": means}


def win_prob(model, a, b):
    ma, mb = model["means"].get(a, model["mean"]), model["means"].get(b, model["mean"])
    return 0.5 * (1 + math.erf((ma - mb) / (2 * model["sd"])))


def allplay_stats(s, through):
    out = defaultdict(lambda: {"w": 0, "l": 0, "t": 0, "exp": 0.0})
    for w in range(1, through + 1):
        if is_playoffs(s, w) or str(w) not in s["weeks"]:
            continue
        sc = [(owner_of(s, m["roster_id"]), m["points"]) for m in s["weeks"][str(w)]["matchups"]
              if m.get("matchup_id") is not None]
        n = len(sc) - 1
        if n < 1:
            continue
        for o, p in sc:
            wins = sum(1 for o2, p2 in sc if o2 != o and p > p2)
            ties = sum(1 for o2, p2 in sc if o2 != o and p == p2)
            r = out[o]
            r["w"] += wins
            r["t"] += ties
            r["l"] += n - wins - ties
            r["exp"] += (wins + 0.5 * ties) / n
    return out


def lineup_report(s, season, week, team):
    slots = s.get("roster_positions") or []
    if not slots:
        return None
    tot = defaultdict(lambda: {"act": 0.0, "opt": 0.0, "flips": 0})
    this = []
    for w in range(1, week + 1):
        for g in week_games(s, season, w):
            for me, mm, opp in ((g["a"], g["ma"], g["pb"]), (g["b"], g["mb"], g["pa"])):
                opt = optimal_points(mm, slots)
                if not opt:
                    continue
                flip = mm["points"] < opp < opt
                t = tot[me]
                t["act"] += mm["points"]
                t["opt"] += opt
                t["flips"] += flip
                if w == week:
                    this.append({"team": team(me), "act": mm["points"], "opt": opt,
                                 "eff": round(100 * mm["points"] / opt, 1), "left": round(opt - mm["points"], 2),
                                 "flip": flip})
    if not this:
        return None
    season_rows = sorted(({"team": team(o), "eff": round(100 * t["act"] / t["opt"], 1),
                           "left": round(t["opt"] - t["act"], 2), "flips": t["flips"]}
                          for o, t in tot.items() if t["opt"]), key=lambda r: -r["eff"])
    return {"best": max(this, key=lambda r: (r["eff"], r["act"])),
            "worst": min(this, key=lambda r: (r["eff"], -r["act"])),
            "flipped": [r for r in this if r["flip"]], "season": season_rows}


def power_scores(arc, season, week):
    s = arc["seasons"][season]
    log, ap = score_log(s, week), allplay_stats(s, week)
    rows = []
    for r in standings(arc, season, week):
        o, games = r["oid"], r["w"] + r["l"] + r["t"]
        pts = [p for _, p in log.get(o, [])]
        if not games or not pts:
            continue
        a = ap[o]
        rows.append({"oid": o, "avg": sum(pts) / len(pts), "recent": sum(pts[-3:]) / len(pts[-3:]),
                     "ap": (a["w"] + 0.5 * a["t"]) / max(1, a["w"] + a["l"] + a["t"]),
                     "win": (r["w"] + 0.5 * r["t"]) / games, "r": r, "luck": (r["w"] + 0.5 * r["t"]) - a["exp"]})
    if not rows:
        return []
    def norm(key):
        lo, hi = min(x[key] for x in rows), max(x[key] for x in rows)
        return {x["oid"]: (x[key] - lo) / (hi - lo) if hi > lo else 0.5 for x in rows}
    na, nr = norm("avg"), norm("recent")
    for x in rows:
        x["score"] = 0.35 * x["ap"] + 0.2 * na[x["oid"]] + 0.2 * nr[x["oid"]] + 0.25 * x["win"]
    rows.sort(key=lambda x: -x["score"])
    for i, x in enumerate(rows, 1):
        x["rank"] = i
    return rows


def playoff_odds(arc, season, week, sims=5000):
    s = arc["seasons"][season]
    pws, spots = s.get("playoff_week_start") or 0, s.get("playoff_teams") or 0
    if not pws or not spots or week >= pws:
        return None
    model = score_model(score_log(s, week))
    if not model:
        return None
    base = {r["oid"]: (r["w"] + 0.5 * r["t"], r["pf"]) for r in standings(arc, season, week)}
    rest = [pairings(s, w) for w in range(week + 1, pws)]
    rng = random.Random(f"odds-{season}-{week}")
    made, top, wins_tot = defaultdict(int), defaultdict(int), defaultdict(float)
    owners = list(base)
    for _ in range(sims):
        wins = {o: base[o][0] for o in owners}
        pf = {o: base[o][1] for o in owners}
        for pairs in rest:
            for a, b in pairs:
                sa = rng.gauss(model["means"].get(a, model["mean"]), model["sd"])
                sb = rng.gauss(model["means"].get(b, model["mean"]), model["sd"])
                pf[a] = pf.get(a, 0) + sa
                pf[b] = pf.get(b, 0) + sb
                wins[a if sa > sb else b] = wins.get(a if sa > sb else b, 0) + 1
        order = sorted(owners, key=lambda o: (-wins[o], -pf[o]))
        for o in order[:spots]:
            made[o] += 1
        top[order[0]] += 1
        for o in owners:
            wins_tot[o] += wins[o]
    return {"spots": spots, "weeks_left": len(rest),
            "teams": sorted(({"oid": o, "playoffs": round(100 * made[o] / sims, 1),
                              "top_seed": round(100 * top[o] / sims, 1),
                              "proj_wins": round(wins_tot[o] / sims, 1)} for o in owners),
                            key=lambda r: (-r["playoffs"], -r["proj_wins"]))}


def preview_next(arc, season, week, team, named_rivals, st_rows):
    s = arc["seasons"][season]
    nxt = week + 1
    pws = s.get("playoff_week_start") or 0
    if pws and nxt >= pws:
        return None
    pairs = pairings(s, nxt)
    model = score_model(score_log(s, week))
    if not pairs or not model:
        return None
    allg = all_games(arc)
    rank = {r["oid"]: r for r in st_rows}
    log = score_log(s, week)
    def form(o):
        pts = [p for _, p in log.get(o, [])][-3:]
        return round(sum(pts) / len(pts), 2) if pts else 0
    lines, best, best_score = [], None, None
    for a, b in pairs:
        pa = win_prob(model, a, b)
        fav, dog, pct = (a, b, pa) if pa >= 0.5 else (b, a, 1 - pa)
        lines.append({"fav": team(fav), "dog": team(dog), "fav_oid": fav, "dog_oid": dog, "pct": round(100 * pct)})
        wa, wb, t, meets = series(allg, a, b, gkey(season, week))
        ra, rb = rank.get(a, {}).get("rank", 99), rank.get(b, {}).get("rank", 99)
        interest = -(ra + rb) - 1.5 * abs(ra - rb) + 0.6 * len(meets) - 10 * abs(pct - 0.5) \
                   + (12 if frozenset((a, b)) in named_rivals else 0)
        if best_score is None or interest > best_score:
            best_score = interest
            last = meets[-1] if meets else None
            best = {"a": team(a), "b": team(b), "a_oid": a, "b_oid": b,
                    "a_record": f"{rank[a]['w']}-{rank[a]['l']}" if a in rank else "", "a_rank": ra,
                    "b_record": f"{rank[b]['w']}-{rank[b]['l']}" if b in rank else "", "b_rank": rb,
                    "a_last3": form(a), "b_last3": form(b),
                    "series": {"a_wins": wa, "b_wins": wb, "ties": t, "meetings": len(meets)},
                    "last_meeting": ({"season": last["season"], "week": last["week"],
                                      "a_pts": last["a_pts"], "b_pts": last["b_pts"]} if last else None),
                    "rivalry": named_rivals.get(frozenset((a, b))),
                    "pick": team(fav), "pick_oid": fav, "pick_pct": round(100 * pct)}
    lines.sort(key=lambda l: -l["pct"])
    return {"week": nxt, "game": best, "lines": lines}


def picks_record(arc, season, week):
    """How the paper's picks from earlier editions this season turned out."""
    s = arc["seasons"][season]
    right = wrong = 0
    last = None
    for k, ed in arc.get("editions", {}).items():
        if ed["season"] != season or ed["week"] >= week:
            continue
        pv = ed["facts"].get("preview")
        if not pv:
            continue
        results = {}
        for g in week_games(s, season, pv["week"]):
            if g["pa"] != g["pb"]:
                results[frozenset((g["a"], g["b"]))] = g["a"] if g["pa"] > g["pb"] else g["b"]
        for l in pv["lines"]:
            w = results.get(frozenset((l["fav_oid"], l["dog_oid"])))
            if w is None:
                continue
            right += w == l["fav_oid"]
            wrong += w != l["fav_oid"]
        if ed["week"] == week - 1 and pv.get("game"):
            gm = pv["game"]
            w = results.get(frozenset((gm["a_oid"], gm["b_oid"])))
            if w is not None:
                last = {"pick": gm["pick"], "other": gm["b"] if gm["pick"] == gm["a"] else gm["a"],
                        "pct": gm["pick_pct"], "correct": w == gm["pick_oid"]}
    return {"right": right, "wrong": wrong, "last_game": last} if (right + wrong or last) else None


def extra_facts(arc, season, week, team, named_rivals, st_rows):
    s = arc["seasons"][season]
    out = {}
    cur = power_scores(arc, season, week)
    prev = {x["oid"]: x["rank"] for x in power_scores(arc, season, week - 1)} if week > 1 else {}
    out["power_rankings"] = [{"rank": x["rank"], "move": (prev.get(x["oid"], x["rank"]) - x["rank"]) if prev else 0,
                              "team": team(x["oid"]),
                              "record": f"{x['r']['w']}-{x['r']['l']}" + (f"-{x['r']['t']}" if x["r"]["t"] else ""),
                              "avg": round(x["avg"], 2), "last3": round(x["recent"], 2),
                              "allplay_pct": round(100 * x["ap"], 1), "luck_wins": round(x["luck"], 1)} for x in cur]
    ap = allplay_stats(s, week)
    luck = []
    for r in st_rows:
        a = ap.get(r["oid"])
        if not a:
            continue
        luck.append({"team": team(r["oid"]), "record": f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else ""),
                     "allplay": f"{a['w']}-{a['l']}" + (f"-{a['t']}" if a["t"] else ""),
                     "luck": round((r["w"] + 0.5 * r["t"]) - a["exp"], 1)})
    out["luck"] = sorted(luck, key=lambda x: -x["luck"])
    odds = playoff_odds(arc, season, week)
    if odds:
        for t in odds["teams"]:
            t["team"] = team(t.pop("oid"))
    out["playoff_odds"] = odds
    out["lineups"] = lineup_report(s, season, week, team)
    out["preview"] = preview_next(arc, season, week, team, named_rivals, st_rows)
    out["picks_record"] = picks_record(arc, season, week)
    log = score_log(s, week)
    out["score_history"] = {"weeks": sorted({w for v in log.values() for w, _ in v}),
                            "teams": {team(o): {str(w): p for w, p in v} for o, v in log.items()}}
    return out


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


SCENES = {
    "nuke": "a mushroom cloud behind him and the nuclear briefcase in front: for a monster game",
    "fire": "surrounded by flames: he's on fire or heating up",
    "crown": "a gold crown on his head in front of royal curtains: king of the week",
    "comeback": "hospital backdrop with a giant green CLEARED stamp: back from injury",
    "bench": "stuck behind a wooden bench with a bucket of popcorn: left on the bench",
    "rain": "a storm cloud raining on him: a dud",
    "bargain": "a clearance-bin price tag: a cheap pickup who delivered",
    "wrecking": "a wrecking ball and caution stripes: part of a blowout",
    "rookie": "a ROOKIE balloon and a baby rattle: a rookie breaking out",
}


def scene_caption(scene, name, mgr, week, rnd):
    options = {
        "nuke": [f"{name} reaches for the launch codes.", f"{name} was not authorized to use this much force."],
        "fire": [f"{name}, currently too hot to handle.", f"Someone get {name} a fire extinguisher."],
        "crown": [f"All hail {name}, king of week {week}.", f"{name} accepts the crown and the gratitude of {mgr}."],
        "comeback": [f"{name}, cleared and back to ruining weekends.", f"The medical staff signed off. The opposing defense did not."],
        "bench": [f"{name} enjoys the game from the best seat in the house: {mgr}'s bench.",
                  f"{name}, pictured doing exactly what {mgr} asked of him."],
        "rain": [f"{name}'s week, summarized.", f"Forecast for {mgr}: cloudy, with a chance of regret."],
        "bargain": [f"Found in the bargain bin: {name}.", f"{mgr} paid clearance prices for {name}. No returns, no regrets."],
        "wrecking": [f"{name} brings the wrecking ball.", f"Demolition crew foreman {name}, at work."],
        "rookie": [f"{name} is too young to know he isn't supposed to be this good.", f"Rookie {name}, already ahead of schedule."],
    }
    return rnd.choice(options.get(scene, [name]))


def template_write(f):
    rnd = random.Random(f"{f['season']}-{f['week']}")
    games = {g["mid"]: g for g in f["games"]}
    aw = f["awards"]
    M = f.get("managers") or {}
    mg = lambda team: M.get(team, team)
    P = f.get("players") or {}
    recs = {r["team"]: f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else "") for r in f["standings"]}

    def line(t):
        d = P.get(t.get("pid")) or {}
        return d.get("stat_line") or f"{fmt(t['pts'])} fantasy points"

    def note(pid, starts):
        for n in (P.get(pid) or {}).get("notes", []):
            if n.startswith(starts):
                return n
        return None

    def series_sentence(g):
        sr = g["series"]
        W, L = g["winner_manager"], g["loser_manager"]
        if sr["meetings"] <= 1:
            return rnd.choice([f"It was the first time {W} and {L} had met.", f"First meeting, first blood to {W}."])
        w, l = sr["winner_wins"], sr["loser_wins"]
        rec = f"{max(w, l)}-{min(w, l)}" + (f"-{sr['ties']}" if sr["ties"] else "")
        if w == l:
            return rnd.choice([f"That evens their history at {rec}.", f"Their all-time series is dead even at {rec}."])
        lead = W if w > l else L
        return rnd.choice([f"{lead} owns the series, {rec}.", f"Head to head, {lead} is up {rec} all time.",
                           f"{lead} still holds the edge in this matchup, {rec}."])

    used = set()
    order = ["comeback", "nuke", "bench_blunder", "revenge", "upset", "rookie", "breakout", "bust",
             "rivalry", "streak", "blowout", "nailbiter"]

    def recap(g):
        W, L = g["winner_manager"], g["loser_manager"]
        score = f"{fmt(g['wp'])}–{fmt(g['lp'])}"
        if g["tie"]:
            return f"{W} and {L} finished dead even at {fmt(g['wp'])}. Nobody wins, everybody complains."
        wt = g["winner_top"][0] if g["winner_top"] else None
        lt = g["loser_top"][0] if g["loser_top"] else None
        angles = {}
        for a in g.get("angles", []):
            angles.setdefault(a["type"], a)
        kind = next((k for k in order if k in angles and k not in used), None) \
            or next((k for k in order if k in angles), "default")
        used.add(kind)
        a = angles.get(kind, {})
        X, who = a.get("player"), a.get("manager")
        tx = next((t for t in g["winner_top"] + g["loser_top"] if t["name"] == X), None) if X else None
        sents = []
        if kind == "comeback" and tx:
            m = re.search(r"missing (\d+) games?(?: \(([^)]+)\))?", note(tx["pid"], "Returned") or "")
            gap = f"missing {m.group(1)} game{'s' if m.group(1) != '1' else ''}" if m else "some time on the sidelines"
            hurt = f" with a {m.group(2)} injury" if m and m.group(2) else ""
            sents.append(rnd.choice([f"After {gap}{hurt}, {X} came back and gave {who} {line(tx)}.",
                                     f"{X} was back, and {who} noticed: {line(tx)} in his first game after {gap}{hurt}."]))
        elif kind == "nuke" and tx:
            other = L if a["side"] == "winner" else W
            sents.append(rnd.choice([f"{X} went nuclear for {who}: {line(tx)}, worth {fmt(tx['pts'])} fantasy points.",
                                     f"Somebody check on {other}. {X} dropped {line(tx)} on him."]))
        elif kind == "bench_blunder":
            b = next(b for b in g["blunders"] if b["cost_game"])
            sents.append(rnd.choice([
                f"{L} had the winning points on his bench. {b['bench']} scored {fmt(b['bench_pts'])} there while "
                f"{b['starter']} managed {fmt(b['starter_pts'])} in the lineup, and the final margin was {fmt(g['margin'])}.",
                f"This one is on {L}. Starting {b['starter']} ({fmt(b['starter_pts'])}) over {b['bench']} "
                f"({fmt(b['bench_pts'])}) cost him a game he lost by {fmt(g['margin'])}."]))
        elif kind == "revenge":
            lm = g["last_meeting"]
            when = f"week {lm['week']}" + (f" of {lm['season']}" if str(lm["season"]) != str(f["season"]) else "")
            sents.append(rnd.choice([f"{W} had been waiting since {when}, when {L} got the better of him. Payback came {score}.",
                                     f"{L} won the last one back in {when}. {W} evened the score, {score}."]))
        elif kind == "upset":
            u = g["upset"]
            sents.append(rnd.choice([f"Nobody had {W} in this one. Sitting {ordinal(u['winner_rank'])} coming in, he took down "
                                     f"{ordinal(u['loser_rank'])}-place {L}, {score}.",
                                     f"The standings said {L}. The scoreboard said {W}, {score}."]))
        elif kind == "rookie" and tx:
            sents.append(rnd.choice([f"{X} does not play like a rookie. He gave {who} {line(tx)}.",
                                     f"Rookie {X} had the best day on the field for {who}: {line(tx)}."]))
        elif kind == "breakout" and tx:
            sents.append(rnd.choice([f"{X} picked a good week for his best game of the season: {line(tx)} for {who}.",
                                     f"Season high for {X}, and {who} needed every bit of it: {line(tx)}."]))
        elif kind == "bust":
            sents.append(rnd.choice([f"{who}'s week went sideways with {X}, who was {a['detail']}.",
                                     f"{X} let {who} down: {a['detail']}."]))
        elif kind == "rivalry":
            sents.append(f"{W} takes this year's edition of {a['name']}, {score}.")
        elif kind == "streak":
            n, won = int(a["streak"][1:]), a["streak"][0] == "W"
            sents.append(rnd.choice([f"{a['manager']} has now {'won' if won else 'lost'} {n} straight.",
                                     f"Make it {n} {'wins' if won else 'losses'} in a row for {a['manager']}."]))
        elif kind == "blowout":
            sents.append(rnd.choice([f"This one was over by Sunday afternoon. {W} beat {L} {score}.",
                                     f"{W} didn't just beat {L}, he buried him: {score}."]))
        elif kind == "nailbiter":
            sents.append(rnd.choice([f"{fmt(g['margin'])} points. That's all that separated {W} and {L} in a {score} finish.",
                                     f"{W} survived, barely: {score} over {L}."]))
        elif wt:
            sents.append(rnd.choice([f"{W} handled {L}, {score}, behind {wt['name']}'s {line(wt)}.",
                                     f"{wt['name']} did the heavy lifting for {W}: {line(wt)}."]))
        else:
            sents.append(f"{W} beat {L}, {score}.")
        if score not in " ".join(sents):
            sents.append(rnd.choice([f"Final: {W} {fmt(g['wp'])}, {L} {fmt(g['lp'])}.", f"{W} won it {score}.",
                                     f"The final was {score}."]))
        if lt and kind != "bench_blunder" and X != lt["name"]:
            sents.append(rnd.choice([f"{L}'s best answer was {lt['name']}, with {line(lt)}.",
                                     f"{lt['name']} kept {L} in it ({line(lt)}).",
                                     f"For {L}, {lt['name']} had {line(lt)}."]))
        elif wt and X != wt["name"] and kind != "default":
            sents.append(f"{wt['name']} chipped in {line(wt)} for {W}.")
        if rnd.random() < 0.6 or not recs.get(g["winner"]):
            sents.append(series_sentence(g))
        else:
            sents.append(rnd.choice([f"{W} moves to {recs[g['winner']]}.", f"{L} falls to {recs.get(g['loser'], '')}."]))
        return " ".join(sents)

    recaps = {}
    for g in f["games"]:
        hed = (f"{g['winner']} and {g['loser']} tie at {fmt(g['wp'])}" if g["tie"] else
               f"{g['winner']} {recap_verb(g['margin'], rnd)} {g['loser']}, {fmt(g['wp'])}–{fmt(g['lp'])}")
        recaps[str(g["mid"])] = {"headline": hed, "body": recap(g)}

    # Front page: pick the biggest story
    hed, dek, focus, lead_scene, lead_pid = None, None, None, None, None
    upsets = [g for g in f["games"] if g["upset"]]
    rival_games = [g for g in f["games"] if g["rivalry"]]
    costly = [g for g in f["games"] if any(b["cost_game"] for b in g["blunders"])]
    if f["records"] and "all-time" in f["records"][0]:
        hed = f"{aw['high']['team']} rewrites the record book with {fmt(aw['high']['pts'])}"
        dek = f["records"][0]
        focus = next((g for g in f["games"] if g["winner"] == aw["high"]["team"]), None)
        lead_scene = "nuke"
    elif upsets:
        g = max(upsets, key=lambda g: g["upset"]["winner_rank"] - g["upset"]["loser_rank"])
        focus, lead_scene = g, "fire"
        hed = f"{g['winner']} stuns {ordinal(g['upset']['loser_rank'])}-place {g['loser']}"
        dek = f"{g['winner_manager']} came in {ordinal(g['upset']['winner_rank'])} and won {fmt(g['wp'])}–{fmt(g['lp'])}."
    elif rival_games:
        g = rival_games[0]
        focus, lead_scene = g, "fire"
        hed = f"{g['winner']} claims {g['rivalry']}"
        dek = f"{g['winner_manager']} beat {g['loser_manager']} {fmt(g['wp'])}–{fmt(g['lp'])}. {series_sentence(g)}"
    elif aw.get("blowout") is not None and games[aw["blowout"]]["margin"] >= 40:
        g = games[aw["blowout"]]
        focus, lead_scene = g, "wrecking"
        hed = f"{g['winner']} {recap_verb(g['margin'], rnd)} {g['loser']} by {fmt(g['margin'])}"
        dek = f"{g['winner_manager']} won {fmt(g['wp'])}–{fmt(g['lp'])}, the most lopsided game of the week."
    elif costly:
        g = costly[0]
        b = next(b for b in g["blunders"] if b["cost_game"])
        focus, lead_scene, lead_pid = g, "bench", b.get("bench_pid")
        hed = f"{b['bench']} watches from the bench as {b['team']} falls"
        dek = f"{mg(b['team'])} left {fmt(b['bench_pts'])} points unused in a {fmt(g['margin'])}-point loss."
    elif aw.get("nailbiter") is not None and games[aw["nailbiter"]]["margin"] < 3:
        g = games[aw["nailbiter"]]
        focus, lead_scene = g, "fire"
        hed = f"{g['winner']} survives {g['loser']} by {fmt(g['margin'])}"
        dek = f"{g['winner_manager']} edged {g['loser_manager']} {fmt(g['wp'])}–{fmt(g['lp'])}, the closest finish of the week."
    elif aw.get("high"):
        hed = f"{aw['high']['team']} leads week {f['week']} with {fmt(aw['high']['pts'])}"
        dek = f"{mg(aw['high']['team'])} posted the top score of the week."
        lead_scene = "crown"
    else:
        hed, dek = f"Week {f['week']} is in the books", "Results from around the league."

    lead = []
    if focus:
        lead.append(recaps[str(focus["mid"])]["body"])
    if aw.get("high"):
        p = aw.get("player_of_week")
        txt = rnd.choice([f"{mg(aw['high']['team'])} had the week's best score at {fmt(aw['high']['pts'])}",
                          f"Nobody scored more than {mg(aw['high']['team'])}'s {fmt(aw['high']['pts'])}"])
        txt += f", while {mg(aw['low']['team'])} brought up the rear with {fmt(aw['low']['pts'])}." if aw.get("low") else "."
        if p:
            pd = P.get(p.get("pid")) or {}
            txt += (f" {p['player']} was the best starter in the league" +
                    (f": {pd['stat_line']}." if pd.get("stat_line") else f" with {fmt(p['pts'])}."))
        lead.append(txt)
    if f["standings"]:
        top = f["standings"][0]
        txt = f"{mg(top['team'])} sits atop the standings at {recs.get(top['team'], '')}."
        if f["streaks"]:
            txt += " " + " ".join(f["streaks"][:2])
        lead.append(txt)
    if f["moves"]["trades"]:
        tr = f["moves"]["trades"][0]
        lead.append("On the trade front: " + "; ".join(
            f"{mg(sd['team'])} gets {', '.join(sd['got']) or 'nothing'}" for sd in tr["sides"]) + ".")
    elif f["waiver_wonder"]:
        w = f["waiver_wonder"]
        lead.append(f"Off the wire, {w['player']} gave {mg(w['team'])} {fmt(w['this_week'])} points this week.")
    if f["previous"] and f["previous"][0].get("headline"):
        lead.append(f"Last week's front page: \u201c{f['previous'][0]['headline']}.\u201d The league has moved on, mostly.")

    col = []
    if aw.get("bad_beat"):
        b = aw["bad_beat"]
        col.append(f"Spare a thought for {mg(b['team'])}. {fmt(b['pts'])} points would have beaten {b['would_beat']} "
                   f"of {b['of']} teams this week, and it still went down as a loss.")
    if aw.get("lucky_win"):
        l = aw["lucky_win"]
        col.append(f"Meanwhile {mg(l['team'])} won with {fmt(l['pts'])}, a score that beats only {l['would_beat']} "
                   f"of {l['of']} teams. Nobody hands back a win, but nobody should brag about this one either.")
    if aw.get("dud"):
        d = aw["dud"]
        col.append(f"And to {mg(d['team'])}, who started {d['player']}: {fmt(d['pts'])} points. That one stings.")
    if not col:
        col.append("A quiet week in the press box. That never lasts.")

    storylines = list(f["records"]) + list(f["streaks"])
    for g in upsets[:1]:
        storylines.append(f"{g['winner_manager']} upset {g['loser_manager']} in week {f['week']}.")
    for t in f["trade_tracker"]:
        if t["leader"] and t["gap"] >= 15:
            storylines.append(f"{mg(t['leader'])} is winning the week-{t['week']} trade by {fmt(t['gap'])} points.")
    if f["pickup_hof"]:
        p = f["pickup_hof"][0]
        storylines.append(f"{p['player']} is the best pickup of the season so far ({mg(p['team'])}, {fmt(p['pts'])} pts).")

    # Photos: one on the front page, at most two in the recaps
    def first_note(pid, starts):
        return bool(note(pid, starts))
    lead_photo = None
    if focus and not lead_pid and focus["winner_top"]:
        lead_pid = focus["winner_top"][0]["pid"]
    if not lead_pid and aw.get("player_of_week"):
        lead_pid, lead_scene = aw["player_of_week"].get("pid"), "crown"
    if lead_pid:
        if first_note(lead_pid, "Returned"):
            lead_scene = "comeback"
        lead_scene = lead_scene or "fire"
        nm = (P.get(lead_pid) or {}).get("name") or pname(lead_pid)
        owner = focus["winner_manager"] if focus else mg(aw.get("player_of_week", {}).get("team", ""))
        if lead_scene == "bench" and focus:
            owner = focus["loser_manager"]
        lead_photo = {"pid": lead_pid, "scene": lead_scene,
                      "caption": scene_caption(lead_scene, nm, owner, f["week"], rnd)}
    recap_photos = {}
    for g in f["games"]:
        if len(recap_photos) >= 2:
            break
        if focus and g["mid"] == focus["mid"]:
            continue
        types = {a["type"]: a for a in g.get("angles", [])}
        pick = None
        if "comeback" in types:
            pick = (types["comeback"]["player"], "comeback", types["comeback"]["manager"])
        elif "nuke" in types:
            pick = (types["nuke"]["player"], "nuke", types["nuke"]["manager"])
        elif "rookie" in types:
            pick = (types["rookie"]["player"], "rookie", types["rookie"]["manager"])
        elif g["mid"] == aw.get("blowout") and g["margin"] >= 30 and g["winner_top"]:
            pick = (g["winner_top"][0]["name"], "wrecking", g["winner_manager"])
        if pick:
            pid = next((t["pid"] for t in g["winner_top"] + g["loser_top"] if t["name"] == pick[0]), None)
            if pid and pid != lead_pid:
                recap_photos[str(g["mid"])] = {"pid": pid, "scene": pick[1],
                                               "caption": scene_caption(pick[1], pick[0], pick[2], f["week"], rnd)}
    caps = {}
    if aw.get("player_of_week"):
        p = aw["player_of_week"]
        caps["player_of_week"] = scene_caption("crown", p["player"], mg(p["team"]), f["week"], rnd)
    if aw.get("dud"):
        d = aw["dud"]
        caps["wanted"] = (f"Last seen putting up {fmt(d['pts'])} points in {mg(d['team'])}'s lineup. "
                          f"If spotted in a starting slot again, alert the commissioner.")
    if aw.get("bench_blunder"):
        b = aw["bench_blunder"]
        caps["missing"] = (f"Last seen scoring {fmt(b['bench_pts'])} points on {mg(b['team'])}'s bench. "
                           f"Reward: one retroactive win, which is not how this works.")
    photos = {"lead": lead_photo, "recaps": recap_photos, "captions": caps}

    # Power ranking one-liners
    blurbs = {}
    pr = f.get("power_rankings") or []
    for x in pr:
        t, avg, l3, lk, who = x["team"], x["avg"], x["last3"], x["luck_wins"], mg(x["team"])
        if x["rank"] == 1:
            line_ = rnd.choice([f"{who} is the team to beat. Everyone else is playing for second until further notice.",
                                f"Averaging {fmt(avg)} a week. {who} has the rest of the league on notice."])
        elif x["rank"] == len(pr):
            line_ = rnd.choice([f"Somebody has to be last. This week, it's {who}.",
                                f"{fmt(avg)} a week. The only direction left for {who} is up."])
        elif lk <= -1:
            line_ = f"Better than the record says: the schedule owes {who} about {abs(lk):.1f} wins."
        elif lk >= 1:
            line_ = f"{who}'s record is writing checks the scoring can't cash ({lk:.1f} wins of pure luck)."
        elif l3 - avg >= 12:
            line_ = f"{who} is heating up: {fmt(l3)} a week over the last three."
        elif avg - l3 >= 12:
            line_ = f"{who} is cooling off fast: {fmt(l3)} a week lately, down from a {fmt(avg)} average."
        elif x["move"] >= 2:
            line_ = f"Up {x['move']} spots, and nobody saw {who} coming."
        elif x["move"] <= -2:
            line_ = f"Down {-x['move']} spots. The slide is on for {who}."
        else:
            line_ = rnd.choice([f"{who} is averaging {fmt(avg)} and doing nothing to change anyone's mind.",
                                f"Solidly in the mix. Solidly unremarkable. That's {who}.",
                                f"{who} is exactly as good as the record says. No more, no less."])
        blurbs[t] = line_

    # Game of the week
    preview = None
    pv = (f.get("preview") or {}).get("game")
    if pv:
        A, B = mg(pv["a"]), mg(pv["b"])
        sr = pv["series"]
        if sr["meetings"]:
            lead_m = A if sr["a_wins"] > sr["b_wins"] else B
            hi, lo = max(sr["a_wins"], sr["b_wins"]), min(sr["a_wins"], sr["b_wins"])
            hist = (f"Their all-time series is tied {hi}-{lo}." if hi == lo
                    else f"{lead_m} leads the all-time series {hi}-{lo}.")
        else:
            hist = "These two have never met."
        preview = {"headline": f"{pv['a']} vs. {pv['b']}" + (f": {pv['rivalry']}" if pv.get("rivalry") else ""),
                   "body": (f"{A} ({pv['a_record']}) has averaged {fmt(pv['a_last3'])} over his last three games; "
                            f"{B} ({pv['b_record']}) has averaged {fmt(pv['b_last3'])}. {hist} "
                            f"The Gazette's pick: {mg(pv['pick'])}, with a {pv['pick_pct']}% chance to win.")}

    return {"headline": hed, "subhead": dek, "lead": lead, "recaps": recaps, "photos": photos,
            "power_blurbs": blurbs, "preview": preview,
            "column": {"title": "From the press box", "body": col}, "storylines": storylines[:6]}


def ai_write(facts, model, key):
    scenes = "\n".join(f'  "{k}": {v}' for k, v in SCENES.items())
    photo_rules = photo_schema = ""
    if SHOW_PHOTOS:
        photo_rules = ("PHOTOS\nPhotos are illustrated scenes with the player's headshot dropped in. Available scenes:\n" + scenes +
                       "\nChoose 1 to 3 photos: always one for \"lead\", plus at most two recaps where a scene genuinely fits the story. "
                       "For a recap photo, choose a candidate from photo_candidates whose \"mid\" matches that game. Captions are one "
                       "funny line that plays off the scene, about the fantasy result only, never the player's appearance or personal life.\n\n")
        photo_schema = ('  "photos": [{"slot": "lead or a game\'s mid", "player": "exact name from photo_candidates", '
                        '"scene": "a scene name", "caption": "..."}],\n')
    system = f"""You are the editor-in-chief of "{facts['paper']}", the weekly newspaper of a fantasy football league among friends. Your voice: {VOICE}.

ACCURACY
- Use only the facts provided. Never invent players, stats, injuries, quotes, trades, or events. Every number you write must appear in the facts.
- Injury and history claims must come from a player's "notes" or "recent_games" in facts.players. If the notes don't say why a player missed games, say he missed them, not why.

NAMES
- In article text (lead, recaps, column, preview, captions, power blurbs), call managers by their names from facts.managers, e.g. "Jack", never "THE crab warriors". Headlines use team names. You may occasionally use a possessive like "Jack's crab warriors" for flavor.

WRITE LIKE A REAL SPORTS SECTION, NOT A BOX SCORE
- Build each recap around a story. Each game has "angles" (comeback from injury, nuke, breakout, rookie, bust, bench blunder, revenge, upset, streak, rivalry, blowout, nailbiter), and facts.players has notes on each player's week: season highs, games missed, projections beaten or missed, positional ranks, recent form, injury designations. Pick the most interesting thread and tell it.
- Describe performances with real football from facts.players[pid].stat_line, e.g. "caught nine balls for 141 yards and two scores". Write numbers in words or digits naturally. Use fantasy point totals sparingly: the final score once, plus at most one other fantasy number per recap.
- Every recap must be built differently. Rotate what you open with: a player's comeback, a manager's decision, a single moment, the standings, the history between the two managers, a projection that went wrong. Vary sentence rhythm and length (2 to 5 sentences). No two recaps may open with the same kind of subject or structure.
- Banned phrases: "led [team] with", "weren't enough", "it was the first meeting", "the all-time series is now", "in a game that", "posted a", "put up", and opening a recap with a team or manager name followed directly by a verb and the score. Never repeat a phrase across recaps.
- The front-page story is a real narrative about the week's biggest story, with context from earlier weeks ("previous", rivalry_watch, trade_tracker, streaks).

{photo_rules}

Return ONLY a JSON object, no markdown fences, shaped exactly like this:
{{"headline": "max 12 words", "subhead": "one sentence",
  "lead": ["3 to 5 paragraphs for the front-page story"],
  "recaps": {{"<mid>": {{"headline": "...", "body": "2 to 5 sentences"}}}},
  "column": {{"title": "...", "body": ["2 or 3 opinion paragraphs"]}},
  "storylines": ["3 to 6 short sentences worth remembering in future editions"],
  "power_blurbs": {{"<team name>": "one-line roast or compliment for every team in power_rankings"}},
  "preview": {{"headline": "...", "body": "3 to 4 sentences previewing preview.game"}},
{photo_schema}  "captions": {{"player_of_week": "...", "wanted": "for the worst starter (awards.dud)", "missing": "for the benched player (awards.bench_blunder)"}}}}
Write one recap per game, keyed by that game's "mid".
Power blurbs: one line per team, keyed by the exact team name, grounded in that team's numbers. Playful roasts are welcome.
Preview: build hype for preview.game and state the paper's pick exactly as given (preview.game.pick at pick_pct percent). If picks_record exists, brag or eat crow about it, especially picks_record.last_game.
Poster captions: the "missing" poster is already headed "Missing: have you seen this player?" and the "wanted" poster "Wanted for crimes against a starting lineup", so don't repeat those phrases."""
    body = json.dumps({"model": model, "max_tokens": 9000, "system": system,
                       "messages": [{"role": "user", "content": "This week's facts:\n" + json.dumps(
                           {k: v for k, v in facts.items()
                            if k != "score_history" and (SHOW_PHOTOS or k != "photo_candidates")})}]}).encode()
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
    bph = base.get("photos") or {}
    ph = {"lead": bph.get("lead"), "recaps": dict(bph.get("recaps") or {}), "captions": dict(bph.get("captions") or {})}
    cands = {c["player"].lower(): c for c in (ai.get("_candidates") or [])}
    mids = {str(g["mid"]) for g in ai.get("_games", [])}
    if isinstance(ai.get("photos"), list):
        picked = {}
        for x in ai["photos"][:3]:
            if not isinstance(x, dict):
                continue
            c = cands.get(str(x.get("player", "")).lower())
            scene, slot = x.get("scene"), str(x.get("slot", ""))
            if not c or scene not in SCENES:
                continue
            if slot == "lead" or (slot in mids and str(c.get("mid")) == slot):
                picked[slot] = {"pid": c["pid"], "scene": scene, "caption": str(x.get("caption") or "")}
        if "lead" in picked:
            ph["lead"] = picked.pop("lead")
        if picked or "lead" in [str(x.get("slot")) for x in ai["photos"] if isinstance(x, dict)]:
            ph["recaps"] = picked
    caps = ai.get("captions") if isinstance(ai.get("captions"), dict) else {}
    for k in ("player_of_week", "wanted", "missing"):
        if isinstance(caps.get(k), str) and caps[k].strip() and k in ph["captions"]:
            ph["captions"][k] = caps[k].strip()
    out["photos"] = ph
    if isinstance(ai.get("power_blurbs"), dict):
        pb = dict(base.get("power_blurbs") or {})
        for k, v in ai["power_blurbs"].items():
            if k in pb and isinstance(v, str) and v.strip():
                pb[k] = v.strip()
        out["power_blurbs"] = pb
    pv = ai.get("preview")
    if base.get("preview") and isinstance(pv, dict) and pv.get("headline") and pv.get("body"):
        out["preview"] = {"headline": str(pv["headline"]), "body": str(pv["body"])}
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
.front{display:grid;grid-template-columns:minmax(0,1fr) 350px;gap:30px;padding:20px 0 24px;border-bottom:4px double var(--rule)}
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
table.stand{font-size:14px}
table.stand td{vertical-align:middle;padding:6px 4px}
table.stand th{padding:4px}
.nw{white-space:nowrap}
.rk{color:var(--muted);width:1.6em}
.who{display:flex;align-items:center;gap:8px;min-width:0}
.av{width:22px;height:22px;border-radius:50%;flex:none;object-fit:cover;background:rgba(0,0,0,.08);filter:grayscale(.3)}
.nm{line-height:1.25;overflow-wrap:anywhere}
.mv{display:inline-block;margin-left:6px;font:600 11px/1 Oswald,'Arial Narrow',sans-serif;white-space:nowrap;vertical-align:1px}
.previously{margin-top:24px}
figure.ph{margin:0 0 14px;break-inside:avoid}
.ph .frame{position:relative;overflow:hidden;background:#cdb8a9;border:1px solid var(--rule)}
.ph img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;object-position:50% 20%;
  filter:grayscale(1) contrast(1.3) brightness(1.04) sepia(.18);transition:filter .35s}
.ph .frame::after{content:"";position:absolute;inset:0;pointer-events:none;mix-blend-mode:multiply;opacity:.32;
  background:radial-gradient(circle,#000 .9px,transparent 1.3px) 0 0/4px 4px}
.ph:hover img{filter:none}
.ph:hover .frame::after{opacity:0}
.ph figcaption{font-size:14px;line-height:1.4;font-style:italic;padding:6px 0 0;border-bottom:1px solid var(--soft);padding-bottom:8px}
.ph.lead-photo img{aspect-ratio:5/4}
.scene .stage{position:relative;aspect-ratio:4/3;overflow:hidden;border:1px solid var(--rule);background:#e7d6c8}
.scene svg.bg,.scene svg.fg{position:absolute;inset:0;width:100%;height:100%;display:block}
.scene .stage::after{display:none}
.ph.scene img.cut{position:absolute;left:50%;bottom:0;height:86%;width:auto;max-width:98%;aspect-ratio:auto;
  transform:translateX(-50%);object-fit:contain;filter:none}
.ph.scene img.cut.round{height:60%;bottom:14%;aspect-ratio:1;border-radius:50%;object-fit:cover;border:4px solid #fff;
  box-shadow:0 4px 14px rgba(0,0,0,.35)}
.ph.scene.s-crown img.cut{height:74%}
.ph.scene.s-bench img.cut{bottom:12%;height:80%}
.ph.scene.s-rain img.cut{height:72%}
.ph.scene.s-wrecking img.cut{left:58%}
.poster .ph.scene img.cut{aspect-ratio:auto}
.poster .ph.scene .stage{aspect-ratio:1}
.ph.thumb{float:right;width:84px;margin:4px 0 6px 12px}
.ph.thumb img{aspect-ratio:1}
.box.feature{grid-column:span 2}
.box.feature .ph.scene.wide{float:left;width:48%;margin:2px 20px 10px 0}
.box.feature .ph.wide{float:left;width:46%;margin:2px 20px 10px 0}
.box.feature .line{clear:both}
.kicker{margin:0 0 4px;font:600 13px/1.2 Oswald,'Arial Narrow',sans-serif;color:var(--win);letter-spacing:.02em}
.posters{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:26px}
.poster{border:1px solid var(--rule);padding:14px 16px 12px;text-align:center;background:rgba(255,255,255,.22)}
.poster .ph{margin:8px auto 10px;max-width:250px}
.poster .ph img{aspect-ratio:1}
.ptitle{margin:0;font:700 34px/1 Oswald,'Arial Narrow',sans-serif;color:var(--head)}
.psub{margin:4px 0 0;font-style:italic;font-size:15px}
.pname{margin:0;font:700 20px/1.2 Oswald,'Arial Narrow',sans-serif;color:var(--head)}
.pstat{margin:2px 0 8px;font:500 14px Oswald,'Arial Narrow',sans-serif;color:var(--muted)}
.pcap{margin:0;font-size:15px;line-height:1.45}
.poster.potw{border:4px double var(--rule)}
.poster.potw .ptitle{font-size:26px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
.poster.missing{border:2px dashed #2f4a6b;background:rgba(240,246,252,.35)}
.poster.missing .ptitle{color:#2f4a6b;font-size:44px;letter-spacing:.06em}
.poster.wanted{background:#e9d3b2;border:3px solid #4a3218;box-shadow:inset 0 0 0 5px #e9d3b2,inset 0 0 0 6px #4a3218}
.poster.wanted .ptitle{font:400 54px/1 'UnifrakturMaguntia',serif;color:#3a2410}
.poster.wanted .ph img{filter:grayscale(1) sepia(.75) contrast(1.25)}
.poster.wanted .ph:hover img{filter:none}
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
.histlink{margin:16px 0 4px;font:600 18px Oswald,'Arial Narrow',sans-serif}
.archive li{padding:14px 0;border-bottom:1px solid var(--soft);list-style:none}
.archive a{text-decoration:none}
.archive a:hover h4{text-decoration:underline}
.archive h4{margin:2px 0;font:700 28px/1.1 Oswald,'Arial Narrow',sans-serif;color:var(--head)}
.archive p{margin:0;font-style:italic}

.tw{overflow-x:auto}
table.power td{vertical-align:top;padding:8px 6px 8px 0}
td.big{font:700 22px/1 Oswald,'Arial Narrow',sans-serif;color:var(--head);width:1.6em}
.tn{font-weight:600}
.pb{display:block;font-style:italic;font-size:14.5px;color:var(--muted);margin-top:2px;line-height:1.4}
.mv.flat{color:var(--muted)}
.note{font-size:14px;color:var(--muted);margin:0 0 10px;line-height:1.45}
.center{text-align:center}
.pos{color:var(--win);font-weight:600}
.neg{color:#8a2a1c;font-weight:600}
.barcell{width:38%}
.bar{position:relative;height:9px;background:rgba(0,0,0,.1);min-width:50px}
.bar span{position:absolute;left:0;top:0;bottom:0;background:var(--win)}
.bar.split{height:12px;background:#8a2a1c;margin:10px 0 4px}
.card{border:1px solid var(--rule);padding:9px 12px;margin-bottom:10px}
.card p{margin:0;font-size:14.5px}
.card.good{border-left:6px solid var(--win)}
.card.bad{border-left:6px solid #8a2a1c}
.clabel{font:600 13px Oswald,'Arial Narrow',sans-serif;color:var(--muted)}
.cteam{font:700 19px/1.2 Oswald,'Arial Narrow',sans-serif;color:var(--head);margin:0}
.flip{font-size:14px;font-style:italic;margin:0 0 10px}
.chart .chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chart button{font:500 13px Oswald,'Arial Narrow',sans-serif;border:1px solid var(--rule);background:transparent;
  color:var(--ink);padding:4px 9px;cursor:pointer;border-radius:0}
.chart button.on{background:var(--c);border-color:var(--c);color:#fff}
.chart button:focus-visible{outline:2px solid var(--win);outline-offset:2px}
.svgwrap{overflow-x:auto}
.chart svg{width:100%;min-width:560px;height:auto;display:block}
.chart .gl{stroke:var(--soft);stroke-width:1}
.chart .ax{font:500 12px Oswald,'Arial Narrow',sans-serif;fill:var(--muted)}
.chart .avg{fill:none;stroke:var(--head);stroke-width:1.5;stroke-dasharray:5 4}
.chart g polyline{fill:none;stroke:var(--ink);stroke-width:1.6;opacity:.35;transition:opacity .2s}
.chart g circle{display:none}
.chart.focus g polyline{opacity:.1}
.chart g.on polyline{stroke:var(--c);stroke-width:3.5;opacity:1}
.chart g.on circle{display:block;fill:var(--c)}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:30px}
.two>div+div{border-left:1px solid var(--soft);padding-left:30px}
.pvh{margin:0 0 10px;font:700 28px/1.1 Oswald,'Arial Narrow',sans-serif;color:var(--head);text-wrap:balance}
.matchup{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;text-align:center;
  border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);padding:10px 0}
.matchup p{margin:0;font-size:14px}
.vs{font:italic 600 18px 'Source Serif 4',Georgia,serif;color:var(--muted)}
.lineitem{display:grid;grid-template-columns:minmax(0,1fr) 30% 3em;gap:10px;align-items:center;padding:7px 0;
  border-bottom:1px solid var(--soft);font-size:15px}
.lineitem span{text-align:right;font:500 14px Oswald,'Arial Narrow',sans-serif}
.hist h3{margin-top:26px}
.h2h-pick{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px}
.h2h-pick select{font:500 16px Oswald,'Arial Narrow',sans-serif;padding:6px 8px;background:transparent;border:1px solid var(--rule);color:var(--ink);max-width:100%}
.h2h-sum{font:700 24px/1.2 Oswald,'Arial Narrow',sans-serif;color:var(--head);margin:6px 0 10px}
@media (max-width:860px){
  .two{grid-template-columns:1fr}
  .two>div+div{border-left:0;padding-left:0}
  .front{grid-template-columns:1fr}
  aside.side{border-left:0;padding-left:0}
  .three{grid-template-columns:1fr}
  .three>div+div{border-left:0;padding-left:0}
  .box.feature{grid-column:auto}
  .box.feature .ph.wide{float:none;width:100%;margin-right:0}
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


ONERR = ("if(this.dataset.b&amp;&amp;this.src!==this.dataset.b){this.src=this.dataset.b}"
         "else{this.closest('figure').style.display='none'}")


def fig(pid, caption="", cls="", alt=""):
    """A newspaper photo of a player (halftone until hovered), hidden if no image loads."""
    if not pid or not SHOW_PHOTOS:
        return ""
    src, backup = photo(pid)
    cap = f"<figcaption>{e(caption)}</figcaption>" if caption else ""
    return (f'<figure class="ph {cls}"><div class="frame"><img src="{e(src)}" data-b="{e(backup)}" '
            f'alt="{e(alt or pname(pid))}" loading="lazy" onerror="{ONERR}"></div>{cap}</figure>')



def score_chart(sh):
    if not sh or len(sh.get("weeks", [])) < 2:
        return ""
    weeks, teams = sh["weeks"], sorted(sh["teams"])
    vals = [p for t in teams for p in sh["teams"][t].values()]
    if not vals:
        return ""
    W, H, L, R, T, B = 760, 300, 46, 16, 14, 30
    step = 20 if max(vals) - min(vals) < 120 else 40
    lo, hi = math.floor(min(vals) / step) * step, math.ceil(max(vals) / step) * step
    hi = hi if hi > lo else lo + step
    x = lambda w: L + (W - L - R) * weeks.index(w) / (len(weeks) - 1)
    y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Weekly scores by team">']
    v = lo
    while v <= hi:
        parts.append(f'<line class="gl" x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text class="ax" x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
        v += step
    for wk in weeks:
        parts.append(f'<text class="ax" x="{x(wk):.1f}" y="{H - 8}" text-anchor="middle">Wk {wk}</text>')
    avg = []
    for wk in weeks:
        pts = [sh["teams"][t][str(wk)] for t in teams if str(wk) in sh["teams"][t]]
        if pts:
            avg.append(f"{x(wk):.1f},{y(sum(pts) / len(pts)):.1f}")
    parts.append(f'<polyline class="avg" points="{" ".join(avg)}"/>')
    chips = []
    for i, t in enumerate(teams):
        pts = [(wk, sh["teams"][t][str(wk)]) for wk in weeks if str(wk) in sh["teams"][t]]
        parts.append(f'<g data-t="{i}"><polyline points="{" ".join(f"{x(wk):.1f},{y(p):.1f}" for wk, p in pts)}"/>')
        for wk, p in pts:
            parts.append(f'<circle cx="{x(wk):.1f}" cy="{y(p):.1f}" r="4"><title>{e(t)}, week {wk}: {fmt(p)}</title></circle>')
        parts.append("</g>")
        chips.append(f'<button type="button" data-t="{i}">{e(t)}</button>')
    parts.append("</svg>")
    return (f'<div class="chart"><div class="chips">{"".join(chips)}</div><div class="svgwrap">{"".join(parts)}</div>'
            f'<p class="note">Dashed line: league average.</p></div>')


SCRIPT = """<script>
document.querySelectorAll('.chart').forEach(function(c){
  var pal=['#1E5B3C','#8a2a1c','#2f4a6b','#9a6b12','#6b2f6b','#2f6b6b'],n=0;
  c.querySelectorAll('button[data-t]').forEach(function(b){
    b.addEventListener('click',function(){
      var on=b.classList.toggle('on'),g=c.querySelector('g[data-t="'+b.dataset.t+'"]');
      if(on){var col=pal[n++%pal.length];b.style.setProperty('--c',col);g.style.setProperty('--c',col);g.parentNode.appendChild(g);}
      g.classList.toggle('on',on);
      c.classList.toggle('focus',!!c.querySelector('button.on'));
    });
  });
});
</script>"""



_UID = [0]


def _trefoil(cx, cy, r):
    parts = []
    for start in (-120, 0, 120):
        a1, a2 = math.radians(start - 30 - 90), math.radians(start + 30 - 90)
        parts.append(f'<path d="M{cx} {cy} L{cx + r * math.cos(a1):.1f} {cy + r * math.sin(a1):.1f} '
                     f'A{r} {r} 0 0 1 {cx + r * math.cos(a2):.1f} {cy + r * math.sin(a2):.1f} Z" fill="#1c1c1c"/>')
    return "".join(parts) + f'<circle cx="{cx}" cy="{cy}" r="{r * .22:.1f}" fill="#1c1c1c" stroke="#facc15" stroke-width="2"/>'


def scene_layers(scene, u):
    """(background svg body, foreground svg body) for a photo scene, drawn on a 400x300 canvas."""
    if scene == "nuke":
        bg = (f'<defs><linearGradient id="nk{u}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a0a0a"/>'
              f'<stop offset=".6" stop-color="#c2410c"/><stop offset="1" stop-color="#fbbf24"/></linearGradient></defs>'
              f'<rect width="400" height="300" fill="url(#nk{u})"/>'
              '<path d="M178 300 C184 240 176 205 168 172 L232 172 C224 205 216 240 222 300Z" fill="#fdba74"/>'
              '<ellipse cx="200" cy="178" rx="78" ry="15" fill="#fb923c"/>'
              '<ellipse cx="200" cy="118" rx="128" ry="64" fill="#fb923c"/><ellipse cx="200" cy="104" rx="100" ry="50" fill="#fdba74"/>'
              '<ellipse cx="200" cy="94" rx="66" ry="30" fill="#fef3c7"/>'
              '<ellipse cx="200" cy="292" rx="210" ry="26" fill="#fde68a" opacity=".85"/>')
        fg = ('<g transform="translate(278 206) rotate(-9)"><path d="M34 16 v-11 h30 v11" fill="none" stroke="#111" stroke-width="7" stroke-linejoin="round"/>'
              '<rect x="0" y="15" width="100" height="70" rx="8" fill="#1f1f1f" stroke="#000" stroke-width="2"/>'
              '<rect x="0" y="40" width="100" height="5" fill="#3a3a3a"/>'
              f'<circle cx="50" cy="52" r="20" fill="#facc15"/>{_trefoil(50, 52, 17)}</g>')
        return bg, fg
    if scene == "fire":
        flames = lambda y, c, o: (f'<path d="M0 300 L0 {y} C30 {y - 60} 45 {y - 20} 60 {y - 70} C75 {y - 20} 95 {y - 40} 110 {y - 95} '
                                  f'C130 {y - 30} 150 {y - 50} 165 {y - 80} C180 {y - 20} 205 {y - 35} 220 {y - 100} C240 {y - 30} 260 {y - 45} '
                                  f'275 {y - 75} C292 {y - 20} 310 {y - 40} 330 {y - 90} C345 {y - 25} 370 {y - 45} 400 {y - 65} L400 300Z" '
                                  f'fill="{c}" opacity="{o}"/>')
        bg = (f'<defs><linearGradient id="fr{u}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1a0606"/>'
              f'<stop offset="1" stop-color="#7c1d0b"/></linearGradient></defs><rect width="400" height="300" fill="url(#fr{u})"/>'
              + flames(250, "#dc2626", .9) + flames(270, "#f97316", .9))
        fg = flames(320, "#f97316", .95) + flames(335, "#facc15", .95)
        return bg, fg
    if scene == "crown":
        stripes = "".join(f'<rect x="{x}" y="0" width="20" height="300" fill="{"#4c1d95" if (x // 20) % 2 else "#5b21b6"}"/>'
                          for x in range(0, 400, 20))
        scallop = "".join(f'<circle cx="{x}" cy="26" r="20" fill="#b8860b"/>' for x in range(10, 410, 40))
        bg = stripes + '<rect width="400" height="26" fill="#b8860b"/>' + scallop + \
             '<rect x="0" y="262" width="400" height="38" fill="#7f1d1d"/>'
        fg = ('<g transform="translate(200 46)"><path d="M-58 34 L-64 -22 L-32 6 L0 -34 L32 6 L64 -22 L58 34 Z" '
              'fill="#facc15" stroke="#a16207" stroke-width="3" stroke-linejoin="round"/>'
              '<rect x="-60" y="30" width="120" height="14" rx="3" fill="#eab308" stroke="#a16207" stroke-width="3"/>'
              '<circle cx="0" cy="12" r="7" fill="#dc2626"/><circle cx="-34" cy="18" r="5" fill="#2563eb"/>'
              '<circle cx="34" cy="18" r="5" fill="#16a34a"/><circle cx="-64" cy="-24" r="5" fill="#fde047"/>'
              '<circle cx="0" cy="-36" r="5" fill="#fde047"/><circle cx="64" cy="-24" r="5" fill="#fde047"/></g>')
        return bg, fg
    if scene == "comeback":
        grid = "".join(f'<line x1="{x}" y1="0" x2="{x}" y2="300" stroke="#b8d8cf" stroke-width="2"/>' for x in range(0, 400, 40)) + \
               "".join(f'<line x1="0" y1="{y}" x2="400" y2="{y}" stroke="#b8d8cf" stroke-width="2"/>' for y in range(0, 300, 40))
        bg = ('<rect width="400" height="300" fill="#dff1ec"/>' + grid +
              '<circle cx="62" cy="62" r="40" fill="#fff" stroke="#dc2626" stroke-width="4"/>'
              '<path d="M54 38h16v16h16v16h-16v16h-16v-16h-16v-16h16z" fill="#dc2626"/>'
              '<polyline points="110,70 160,70 172,40 186,100 200,56 212,70 400,70" fill="none" stroke="#10b981" stroke-width="4"/>')
        fg = ('<g transform="translate(286 238) rotate(-14)" opacity=".92"><rect x="-82" y="-27" width="164" height="54" rx="6" fill="none" '
              'stroke="#15803d" stroke-width="6"/><text x="0" y="14" text-anchor="middle" font-family="Oswald,Impact,sans-serif" '
              'font-weight="700" font-size="38" fill="#15803d" letter-spacing="2">CLEARED</text></g>'
              '<g transform="translate(26 236) rotate(35)"><rect x="0" y="0" width="90" height="26" rx="13" fill="#e7c9a1"/>'
              '<rect x="32" y="2" width="26" height="22" fill="#f5e1c4"/></g>')
        return bg, fg
    if scene == "bench":
        crowd = "".join(f'<circle cx="{(i * 37) % 400}" cy="{20 + (i * 53) % 100}" r="9" fill="{c}"/>'
                        for i, c in zip(range(60), ["#334155", "#64748b", "#94a3b8", "#475569"] * 15))
        bg = ('<rect width="400" height="300" fill="#1e293b"/>' + crowd +
              '<rect y="130" width="400" height="170" fill="#15803d"/>'
              + "".join(f'<rect x="{x}" y="130" width="40" height="170" fill="#16a34a"/>' for x in range(0, 400, 80)))
        fg = ('<rect x="20" y="214" width="360" height="18" rx="3" fill="#92400e"/><rect x="20" y="236" width="360" height="18" rx="3" fill="#a16207"/>'
              '<rect x="20" y="258" width="360" height="12" rx="3" fill="#92400e"/>'
              '<rect x="50" y="270" width="14" height="30" fill="#78350f"/><rect x="336" y="270" width="14" height="30" fill="#78350f"/>'
              '<g transform="translate(318 168)"><path d="M0 0 h56 l-8 62 h-40 z" fill="#fff"/>'
              '<path d="M10 0 h10 l-3 62 h-9 z M34 0 h10 l-4 62 h-9 z" fill="#dc2626"/>'
              + "".join(f'<circle cx="{x}" cy="{y}" r="8" fill="#fde68a" stroke="#eab308"/>'
                        for x, y in ((6, -2), (20, -8), (34, -4), (48, -6), (14, -16), (40, -16), (27, -20))) + '</g>')
        return bg, fg
    if scene == "rain":
        bg = (f'<defs><linearGradient id="rn{u}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#475569"/>'
              f'<stop offset="1" stop-color="#94a3b8"/></linearGradient></defs><rect width="400" height="300" fill="url(#rn{u})"/>')
        drops = "".join(f'<line x1="{x}" y1="{y}" x2="{x - 6}" y2="{y + 18}" stroke="#bfdbfe" stroke-width="3" stroke-linecap="round"/>'
                        for x, y in ((120, 80), (150, 120), (180, 90), (210, 130), (240, 85), (270, 118), (300, 92),
                                     (135, 160), (195, 170), (255, 165), (165, 200), (285, 205), (225, 210)))
        fg = (drops + '<g fill="#334155"><circle cx="140" cy="58" r="34"/><circle cx="190" cy="40" r="44"/><circle cx="245" cy="50" r="38"/>'
              '<circle cx="290" cy="64" r="28"/><rect x="120" y="58" width="190" height="34" rx="17"/></g>'
              '<path d="M200 92 l-14 26 h12 l-8 24 l22 -32 h-12 l10 -18 z" fill="#facc15"/>')
        return bg, fg
    if scene == "bargain":
        stripes = "".join(f'<path d="M{x} 0 l40 0 l-80 300 l-40 0 z" fill="#fde047"/>' for x in range(-40, 480, 80))
        bg = ('<rect width="400" height="300" fill="#facc15"/>' + stripes +
              '<rect x="0" y="0" width="400" height="56" fill="#dc2626"/>'
              '<text x="200" y="40" text-anchor="middle" font-family="Oswald,Impact,sans-serif" font-weight="700" '
              'font-size="34" fill="#fff" letter-spacing="4">CLEARANCE</text>')
        fg = ('<line x1="96" y1="196" x2="150" y2="150" stroke="#78350f" stroke-width="3"/>'
              '<g transform="translate(20 196) rotate(-16)"><path d="M0 14 L22 0 H128 V80 H22 L0 66 Z" fill="#fff7ed" stroke="#9a3412" stroke-width="3"/>'
              '<circle cx="18" cy="40" r="6" fill="#facc15" stroke="#9a3412" stroke-width="2"/>'
              '<text x="76" y="38" text-anchor="middle" font-family="Oswald,Impact,sans-serif" font-weight="700" font-size="20" fill="#9a3412">PRICE</text>'
              '<text x="76" y="66" text-anchor="middle" font-family="Oswald,Impact,sans-serif" font-weight="700" font-size="24" fill="#dc2626">STEAL</text></g>')
        return bg, fg
    if scene == "wrecking":
        stripes = "".join(f'<path d="M{x} 262 l24 0 l-24 38 l-24 0 z" fill="#111"/>' for x in range(0, 440, 48))
        bg = ('<rect width="400" height="300" fill="#cbd5e1"/><rect y="200" width="400" height="62" fill="#94a3b8"/>'
              '<g fill="#e2e8f0" opacity=".9"><circle cx="60" cy="210" r="34"/><circle cx="110" cy="200" r="26"/>'
              '<circle cx="340" cy="206" r="38"/><circle cx="300" cy="214" r="24"/></g>'
              '<rect y="262" width="400" height="38" fill="#facc15"/>' + stripes)
        fg = ('<line x1="0" y1="0" x2="74" y2="96" stroke="#1f2937" stroke-width="5" stroke-dasharray="10 4"/>'
              '<circle cx="84" cy="122" r="42" fill="#1f2937"/><circle cx="70" cy="106" r="11" fill="#475569"/>')
        return bg, fg
    if scene == "rookie":
        stars = "".join(f'<path d="M{x} {y} l5 12 13 1 -10 8 4 13 -12 -7 -12 7 4 -13 -10 -8 13 -1z" fill="#fef9c3"/>'
                        for x, y in ((40, 40), (340, 50), (90, 140), (320, 160), (200, 24), (40, 230)))
        bg = '<rect width="400" height="300" fill="#bfdbfe"/>' + stars
        fg = ('<path d="M330 150 C326 190 340 220 330 300" fill="none" stroke="#334155" stroke-width="2"/>'
              '<ellipse cx="330" cy="104" rx="52" ry="58" fill="#ef4444"/><path d="M322 160 h16 l-8 -8z" fill="#ef4444"/>'
              '<text x="330" y="112" text-anchor="middle" font-family="Oswald,Impact,sans-serif" font-weight="700" font-size="22" fill="#fff">ROOKIE</text>'
              '<g transform="translate(30 220) rotate(-25)"><rect x="0" y="22" width="56" height="10" rx="5" fill="#f9a8d4"/>'
              '<circle cx="70" cy="27" r="20" fill="#f9a8d4" stroke="#db2777" stroke-width="3"/><circle cx="-6" cy="27" r="10" fill="#f9a8d4"/></g>')
        return bg, fg
    return '<rect width="400" height="300" fill="#e7d6c8"/>', ""


SCENE_ONERR = ("if(this.dataset.b){this.src=this.dataset.b;this.dataset.b='';this.classList.add('round')}"
               "else{this.closest('figure').style.display='none'}")


def scene_fig(pid, scene, caption="", cls=""):
    """A photo illustration: drawn scene, player cutout, props on top."""
    if not pid or not SHOW_PHOTOS:
        return ""
    if scene not in SCENES:
        return fig(pid, caption, cls)
    _UID[0] += 1
    bg, fg = scene_layers(scene, _UID[0])
    src, backup = photo(pid)
    round_cls = " round" if not str(pid).isalpha() and "sleepercdn" in src else ""
    cap = f"<figcaption>{e(caption)}</figcaption>" if caption else ""
    return (f'<figure class="ph scene s-{scene} {cls}"><div class="stage">'
            f'<svg class="bg" viewBox="0 0 400 300" preserveAspectRatio="xMidYMid slice" aria-hidden="true">{bg}</svg>'
            f'<img class="cut{round_cls}" src="{e(src)}" data-b="{e(backup)}" alt="{e(pname(pid))}" loading="lazy" onerror="{SCENE_ONERR}">'
            f'<svg class="fg" viewBox="0 0 400 300" preserveAspectRatio="xMidYMid slice" aria-hidden="true">{fg}</svg>'
            f'</div>{cap}</figure>')


def page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(title)}</title>{FONTS}<style>{CSS}</style></head>'
            f'<body><div class="sheet">{body}</div>{SCRIPT}</body></html>')


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
    photos = w.get("photos") or {}
    caps = photos.get("captions") or {}
    lead_ph = photos.get("lead") or {}
    lead_fig = scene_fig(lead_ph.get("pid"), lead_ph.get("scene") or "plain", lead_ph.get("caption", ""), "lead-photo")
    paras = "".join(f"<p>{e(p)}</p>" for p in w["lead"])
    rows = []
    for r in f["standings"]:
        mv = r["move"]
        badge = (f'<span class="mv up" title="Up {mv}">&#9650;{mv}</span>' if mv > 0 else
                 f'<span class="mv down" title="Down {-mv}">&#9660;{-mv}</span>' if mv < 0 else "")
        rec = f"{r['w']}&#8209;{r['l']}" + (f"&#8209;{r['t']}" if r["t"] else "")
        av = (f'<img class="av" src="{e(r["avatar"])}" alt="" loading="lazy" onerror="this.style.visibility=\'hidden\'">'
              if r.get("avatar") else '<span class="av"></span>')
        rows.append(f"<tr><td class='n rk'>{r['rank']}</td><td><div class='who'>{av}<span class='nm'>{e(r['team'])}"
                    f"{badge}</span></div></td><td class='n nw'>{rec}</td>"
                    f"<td class='n nw'>{r['pf']:.1f}</td><td class='n nw'>{e(r['streak'])}</td></tr>")
    prev_html = ""
    if prev_key and f["previous"]:
        p = f["previous"][0]
        prev_html = (f'<div class="previously"><h3>Previously</h3>'
                     f'<p><a href="{fname(*prev_key.split("-"))}">{e(p["headline"] or "Last week")}</a></p>'
                     + "".join(f"<p>{e(s)}</p>" for s in p["storylines"][:2]) + "</div>")
    out.append(f'<section class="front"><article><h2 class="hed">{e(w["headline"])}</h2>'
               f'<p class="dek">{e(w["subhead"])}</p><p class="byline">By the editor</p>'
               f'<div class="cols">{lead_fig}{paras}</div></article>'
               f'<aside class="side"><h3>Standings</h3><table class="stand"><thead><tr><th class="n">#</th><th>Team</th>'
               f'<th class="n">Rec</th><th class="n">PF</th><th class="n">Strk</th></tr></thead>'
               f'<tbody>{"".join(rows)}</tbody></table>{prev_html}</aside></section>')

    # Box scores
    blowout_mid, close_mid = aw.get("blowout"), aw.get("nailbiter")
    out.append('<section class="section"><h3>Around the league <small>Every game, every grudge</small></h3><div class="grid">')
    for g in f["games"]:
        rc = w["recaps"].get(str(g["mid"]), {"headline": "", "body": ""})
        kickers = []
        if g["mid"] == blowout_mid and g["margin"] >= 30:
            kickers.append("Blowout")
        if g["mid"] == close_mid and g["margin"] < 10:
            kickers.append("Photo finish")
        if g.get("upset"):
            kickers.append("Upset")
        if g.get("rivalry"):
            kickers.append(g["rivalry"])
        if any(b.get("cost_game") for b in g.get("blunders", [])):
            kickers.append("Bench blunder")
        kick = f'<p class="kicker">{e(" / ".join(kickers))}</p>' if kickers else ""
        rp = (photos.get("recaps") or {}).get(str(g["mid"]))
        pic = scene_fig(rp["pid"], rp.get("scene") or "plain", rp.get("caption", ""), "wide") if rp and rp.get("pid") else ""
        feature = bool(pic)
        perf = "; ".join(f"{t['name']} {fmt(t['pts'])}" for t in g["winner_top"][:2])
        perf_l = "; ".join(f"{t['name']} {fmt(t['pts'])}" for t in g["loser_top"][:2])
        out.append(f'<article class="box{" feature" if feature else ""}" id="g{g["mid"]}">{kick}{pic}'
                   f'<h4>{e(rc["headline"])}</h4><p>{e(rc["body"])}</p>'
                   f'<div class="line"><div><span class="{"" if g["tie"] else "up"}">{e(g["winner"])}</span><span>{fmt(g["wp"])}</span></div>'
                   f'<div><span>{e(g["loser"])}</span><span>{fmt(g["lp"])}</span></div></div>'
                   f'<p class="perf">{e(perf)}<br>{e(perf_l)}</p></article>')
    out.append("</div></section>")

    # Photo desk: player of the week, missing (benched), wanted (worst starter)
    posters = []
    if aw.get("player_of_week") and aw["player_of_week"].get("pid"):
        p = aw["player_of_week"]
        posters.append(f'<div class="poster potw"><p class="ptitle">Player of the week</p>{scene_fig(p["pid"], "crown")}'
                       f'<p class="pname">{e(p["player"])}</p><p class="pstat">{fmt(p["pts"])} pts for {e(p["team"])}</p>'
                       f'<p class="pcap">{e(caps.get("player_of_week", ""))}</p></div>')
    b = aw.get("bench_blunder")
    if b and b.get("bench_pid"):
        posters.append(f'<div class="poster missing"><p class="ptitle">Missing</p>'
                       f'<p class="psub">Have you seen this {e(b.get("bench_pos") or "player")}?</p>{scene_fig(b["bench_pid"], "bench")}'
                       f'<p class="pname">{e(b["bench"])}</p><p class="pstat">{fmt(b["bench_pts"])} pts on the bench</p>'
                       f'<p class="pcap">{e(caps.get("missing", ""))}</p></div>')
    d = aw.get("dud")
    if d and d.get("pid"):
        posters.append(f'<div class="poster wanted"><p class="ptitle">Wanted</p>'
                       f'<p class="psub">For crimes against a starting lineup</p>{fig(d["pid"])}'
                       f'<p class="pname">{e(d["player"])}</p><p class="pstat">{fmt(d["pts"])} pts for {e(d["team"])}</p>'
                       f'<p class="pcap">{e(caps.get("wanted", ""))}</p></div>')
    if posters:
        desk = "The photo desk" if SHOW_PHOTOS else "Heroes and villains"
        out.append(f'<section class="section"><h3>{desk} <small>Heroes, fugitives, and the missing</small></h3>'
                   f'<div class="posters">{"".join(posters)}</div></section>')

    # Power rankings
    pr = f.get("power_rankings") or []
    blurbs = w.get("power_blurbs") or {}
    if pr:
        rows = []
        for x in pr:
            mv = x["move"]
            badge = (f'<span class="mv up">&#9650;{mv}</span>' if mv > 0 else
                     f'<span class="mv down">&#9660;{-mv}</span>' if mv < 0 else '<span class="mv flat">&ndash;</span>')
            rows.append(f"<tr><td class='n big'>{x['rank']}</td><td class='nw'>{badge}</td>"
                        f"<td><span class='tn'>{e(x['team'])}</span><span class='pb'>{e(blurbs.get(x['team'], ''))}</span></td>"
                        f"<td class='n nw'>{x['record'].replace('-', '&#8209;')}</td><td class='n nw'>{x['avg']:.1f}</td>"
                        f"<td class='n nw'>{x['last3']:.1f}</td></tr>")
        out.append('<section class="section"><h3>Power rankings <small>Record, scoring, recent form, and luck, blended</small></h3>'
                   '<div class="tw"><table class="power"><thead><tr><th class="n">#</th><th></th><th>Team</th>'
                   '<th class="n">Rec</th><th class="n">Avg</th><th class="n">Last 3</th></tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table></div></section>')

    # By the numbers: luck, playoff odds, lineups
    cols = []
    if f.get("luck"):
        rows = "".join(
            f"<tr><td>{e(x['team'])}</td><td class='n nw'>{x['record'].replace('-', '&#8209;')}</td>"
            f"<td class='n nw'>{x['allplay'].replace('-', '&#8209;')}</td>"
            f"<td class='n nw {'pos' if x['luck'] > 0 else 'neg' if x['luck'] < 0 else ''}'>{x['luck']:+.1f}</td></tr>"
            for x in f["luck"])
        cols.append('<div><h3>Luck standings</h3><p class="note">All-play is the record each team would have if it '
                    'played everyone, every week. Luck is wins above or below what that predicts.</p>'
                    '<table><thead><tr><th>Team</th><th class="n">Rec</th><th class="n">All-play</th><th class="n">Luck</th>'
                    f'</tr></thead><tbody>{rows}</tbody></table></div>')
    odds = f.get("playoff_odds")
    if odds:
        rows = "".join(
            f"<tr><td>{e(x['team'])}</td><td class='barcell'><div class='bar'><span style='width:{x['playoffs']}%'></span></div></td>"
            f"<td class='n nw'>{x['playoffs']:.0f}%</td><td class='n nw'>{x['proj_wins']:.1f}</td></tr>"
            for x in odds["teams"])
        cols.append(f'<div><h3>Playoff odds</h3><p class="note">{odds["spots"]} playoff spots, {odds["weeks_left"]} '
                    f'regular-season week{"s" if odds["weeks_left"] != 1 else ""} left, 5,000 simulated finishes.</p>'
                    '<table><thead><tr><th>Team</th><th></th><th class="n">Odds</th><th class="n">Proj W</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table></div>')
    lu = f.get("lineups")
    if lu:
        b, wst = lu["best"], lu["worst"]
        flips = "".join(f'<p class="flip">{e(x["team"])} left enough on the bench to win: best lineup {fmt(x["opt"])}, '
                        f'actual {fmt(x["act"])}.</p>' for x in sorted(lu["flipped"], key=lambda r: -r["left"])[:2])
        rows = "".join(f"<tr><td>{e(x['team'])}</td><td class='n nw'>{x['eff']:.1f}%</td>"
                       f"<td class='n nw'>{x['left']:.1f}</td><td class='n nw'>{x['flips']}</td></tr>" for x in lu["season"])
        cols.append('<div><h3>The lineup report</h3>'
                    f'<div class="card good"><p class="clabel">Manager of the week</p><p class="cteam">{e(b["team"])}</p>'
                    f'<p>{b["eff"]:.1f}% of the best possible lineup ({fmt(b["act"])} of {fmt(b["opt"])})</p></div>'
                    f'<div class="card bad"><p class="clabel">Dunce cap</p><p class="cteam">{e(wst["team"])}</p>'
                    f'<p>{wst["eff"]:.1f}%, leaving {fmt(wst["left"])} points on the bench</p></div>{flips}'
                    '<table><thead><tr><th>Season</th><th class="n">Eff</th><th class="n">Benched</th><th class="n" title="Losses a best lineup would have won">Blown</th>'
                    f'</tr></thead><tbody>{rows}</tbody></table></div>')
    if cols:
        out.append(f'<section class="section three">{"".join(cols)}</section>')

    # Score chart
    chart = score_chart(f.get("score_history"))
    if chart:
        out.append('<section class="section"><h3>The season so far <small>Tap teams to highlight their weekly scores</small></h3>'
                   f'{chart}</section>')

    # Next week
    pv, pvw = f.get("preview"), w.get("preview")
    if pv and pv.get("game") and pvw:
        g = pv["game"]
        pa = g["pick_pct"] if g["pick"] == g["a"] else 100 - g["pick_pct"]
        rec = f.get("picks_record")
        rec_html = ""
        if rec:
            lg = rec.get("last_game")
            if lg:
                verdict = "Called it." if lg["correct"] else "Swing and a miss."
                rec_html += (f'<p class="note">Last week\u2019s game of the week: we picked {e(lg["pick"])} '
                             f'({lg["pct"]}%) over {e(lg["other"])}. {verdict}</p>')
            if rec["right"] + rec["wrong"]:
                rec_html += f'<p class="note">The Gazette\u2019s picks this season: {rec["right"]}-{rec["wrong"]}.</p>'
        lines = "".join(
            f'<div class="lineitem"><div><b>{e(l["fav"])}</b> over {e(l["dog"])}</div>'
            f'<div class="bar"><span style="width:{l["pct"]}%"></span></div><span class="nw">{l["pct"]}%</span></div>'
            for l in pv["lines"])
        out.append(f'<section class="section two"><div><h3>Next week: game of the week</h3>'
                   f'<h4 class="pvh">{e(pvw["headline"])}</h4>'
                   f'<div class="matchup"><div><p class="cteam">{e(g["a"])}</p><p>{e(g["a_record"])}, {ordinal(g["a_rank"])} place</p></div>'
                   f'<div class="vs">vs.</div><div><p class="cteam">{e(g["b"])}</p><p>{e(g["b_record"])}, {ordinal(g["b_rank"])} place</p></div></div>'
                   f'<div class="bar split"><span style="width:{pa}%"></span></div>'
                   f'<p class="note center">{e(g["a"])} {pa}% &middot; {e(g["b"])} {100 - pa}%</p>'
                   f'<p>{e(pvw["body"])}</p></div>'
                   f'<div><h3>Week {pv["week"]} lines <small>Win chances from season scoring</small></h3>{lines}{rec_html}</div></section>')

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
    out.append(f'<footer>{nav_prev}<span><a href="index.html">All editions</a> &middot; '
               f'<a href="history.html">League history</a></span>{nav_next}</footer>')
    return page(f"{f['paper']}: week {f['week']}, {f['season']}", "".join(out))



def season_trades(s, season, through, team):
    out = []
    for wk in sorted(int(k) for k in s["weeks"]):
        if wk > through:
            continue
        for t in s["weeks"][str(wk)]["transactions"]:
            if t["type"] != "trade":
                continue
            sides = []
            for rid in t["roster_ids"]:
                got = [p for p, to in t["adds"].items() if to == rid]
                pts, _ = started_points(s, rid, got, wk, through)
                sides.append({"team": team(owner_of(s, rid)), "pts": pts,
                              "got": [pname(p) for p in got] + [pick_label(p) for p in t["picks"] if p["to"] == rid]})
            if len(sides) >= 2:
                sides.sort(key=lambda x: -x["pts"])
                out.append({"season": season, "week": wk, "sides": sides, "gap": round(sides[0]["pts"] - sides[-1]["pts"], 2)})
    return out


def render_history(arc, paper):
    seasons = sorted(arc["seasons"], key=int)
    if not seasons:
        return page(f"{paper}: league history", "")

    def lookup(oid, field):
        for k in reversed(seasons):
            o = arc["seasons"][k]["owners"].get(oid)
            if o:
                return real_name(o) if field == "name" else o[field]
        return "A former manager"
    team = lambda o: lookup(o, "team")
    manager = lambda o: lookup(o, "name")

    allg = all_games(arc)
    # Champions
    champ_rows, titles = [], defaultdict(int)
    for k in reversed(seasons):
        s = arc["seasons"][k]
        ch = s.get("champion")
        if ch:
            wo = owner_of(s, ch["winner"])
            titles[wo] += 1
            ru = team(owner_of(s, ch["runner_up"])) if ch.get("runner_up") else ""
            champ_rows.append(f"<tr><td class='n'>{e(k)}</td><td><b>{e(team(wo))}</b> <span class='perf'>{e(manager(wo))}</span></td><td>{e(ru)}</td></tr>")
        else:
            champ_rows.append(f"<tr><td class='n'>{e(k)}</td><td colspan='2'><i>{'In progress' if not s.get('complete') else 'Not recorded'}</i></td></tr>")

    # All-time standings (regular season)
    rec = defaultdict(lambda: {"w": 0, "l": 0, "t": 0, "pf": 0.0, "seasons": set()})
    for g in allg:
        if g["playoffs"]:
            continue
        for me, pm, po in ((g["a"], g["pa"], g["pb"]), (g["b"], g["pb"], g["pa"])):
            r = rec[me]
            r["pf"] += pm
            r["seasons"].add(g["season"])
            r["w" if pm > po else "l" if pm < po else "t"] += 1
    rows = sorted(rec.items(), key=lambda kv: (-(kv[1]["w"] + 0.5 * kv[1]["t"]) / max(1, kv[1]["w"] + kv[1]["l"] + kv[1]["t"]), -kv[1]["pf"]))
    at_rows = "".join(
        f"<tr><td><b>{e(team(o))}</b><span class='pb'>{e(manager(o))}</span></td>"
        f"<td class='n nw'>{r['w']}&#8209;{r['l']}" + (f"&#8209;{r['t']}" if r["t"] else "") + "</td>"
        f"<td class='n nw'>{100 * (r['w'] + 0.5 * r['t']) / max(1, r['w'] + r['l'] + r['t']):.1f}%</td>"
        f"<td class='n nw'>{r['pf']:.1f}</td><td class='n'>{len(r['seasons'])}</td><td class='n'>{titles.get(o, 0) or ''}</td></tr>"
        for o, r in rows)

    # Records
    recs = []
    if allg:
        scores = [(g["pa"], g["a"], g["b"], g) for g in allg] + [(g["pb"], g["b"], g["a"], g) for g in allg]
        hi = max(scores, key=lambda x: x[0])
        lo = min(scores, key=lambda x: x[0])
        recs.append(("Highest score", f"{team(hi[1])}, {fmt(hi[0])} vs. {team(hi[2])} (week {hi[3]['week']}, {hi[3]['season']})"))
        recs.append(("Lowest score", f"{team(lo[1])}, {fmt(lo[0])} vs. {team(lo[2])} (week {lo[3]['week']}, {lo[3]['season']})"))
        decided = [g for g in allg if g["pa"] != g["pb"]]
        if decided:
            def desc(g):
                W, L = (g["a"], g["b"]) if g["pa"] > g["pb"] else (g["b"], g["a"])
                return (f"{team(W)} over {team(L)}, {fmt(max(g['pa'], g['pb']))}–{fmt(min(g['pa'], g['pb']))} "
                        f"(week {g['week']}, {g['season']})")
            bl = max(decided, key=lambda g: abs(g["pa"] - g["pb"]))
            cl = min(decided, key=lambda g: abs(g["pa"] - g["pb"]))
            recs.append(("Biggest blowout", f"{fmt(abs(bl['pa'] - bl['pb']))} points: {desc(bl)}"))
            recs.append(("Closest game", f"{fmt(abs(cl['pa'] - cl['pb']))} points: {desc(cl)}"))
        best_season = None
        for k in seasons:
            s = arc["seasons"][k]
            last = max((int(w) for w in s["weeks"]), default=0)
            for r in standings(arc, k, last) if last else []:
                if best_season is None or r["pf"] > best_season[0]:
                    best_season = (r["pf"], r["oid"], k, r["w"], r["l"])
        if best_season:
            recs.append(("Most points in a regular season",
                         f"{team(best_season[1])}, {best_season[0]:.2f} in {best_season[2]} ({best_season[3]}-{best_season[4]})"))
    rec_html = "<dl>" + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in recs) + "</dl>"

    # Lopsided trades
    trades = []
    for k in seasons:
        s = arc["seasons"][k]
        last = max((int(w) for w in s["weeks"]), default=0)
        trades += season_trades(s, k, last, team)
    trades.sort(key=lambda t: -t["gap"])
    tr_rows = "".join(
        f"<tr><td class='nw'>Wk {t['week']}, {e(t['season'])}</td><td>"
        + "<br>".join(f"<b>{e(sd['team'])}</b> got {e(', '.join(sd['got']) or 'nothing')}: {fmt(sd['pts'])} pts" for sd in t["sides"])
        + f"</td><td class='n nw'>{fmt(t['gap'])}</td></tr>" for t in trades[:5])
    tr_html = (f"<table><thead><tr><th>When</th><th>Deal and points started after</th><th class='n'>Gap</th></tr></thead>"
               f"<tbody>{tr_rows}</tbody></table>") if trades else "<p>No trades on record yet.</p>"

    # Head-to-head data
    oids = sorted({o for g in allg for o in (g["a"], g["b"])}, key=lambda o: team(o).lower())
    idx = {o: i for i, o in enumerate(oids)}
    data = {"teams": [team(o) for o in oids],
            "games": [[int(g["season"]), g["week"], idx[g["a"]], idx[g["b"]], g["pa"], g["pb"], int(g["playoffs"])] for g in allg]}
    pair_n = defaultdict(int)
    for g in allg:
        pair_n[tuple(sorted((idx[g["a"]], idx[g["b"]])))] += 1
    da, db = max(pair_n, key=pair_n.get) if pair_n else (0, 1 if len(oids) > 1 else 0)
    opts_a = "".join(f'<option value="{i}"{" selected" if i == da else ""}>{e(t)}</option>' for i, t in enumerate(data["teams"]))
    opts_b = "".join(f'<option value="{i}"{" selected" if i == db else ""}>{e(t)}</option>' for i, t in enumerate(data["teams"]))
    payload = json.dumps(data).replace("</", "<\\/")
    h2h = (f'<div class="h2h-pick"><select id="h2h-a" aria-label="First team">{opts_a}</select><span class="vs">vs.</span>'
           f'<select id="h2h-b" aria-label="Second team">{opts_b}</select></div><div id="h2h-out" aria-live="polite"></div>'
           f'<script type="application/json" id="h2h-data">{payload}</script>' + H2H_JS)

    body = masthead(paper, "<b>League history</b>Since " + e(seasons[0]),
                    f"<b>{len(seasons)} season{'s' if len(seasons) != 1 else ''}</b>{len(allg)} games",
                    ["The record book", "Champions, records, and grudges", ""])
    body += (f'<div class="hist"><section class="section"><h3>Head-to-head lookup <small>Pick any two teams</small></h3>{h2h}</section>'
             f'<section class="section two"><div><h3>Champions</h3><table><thead><tr><th class="n">Season</th><th>Champion</th>'
             f'<th>Runner-up</th></tr></thead><tbody>{"".join(champ_rows)}</tbody></table>'
             f'<h3>All-time records</h3>{rec_html}</div>'
             f'<div><h3>All-time standings <small>Regular season</small></h3><div class="tw"><table><thead><tr><th>Team</th>'
             f'<th class="n">W-L</th><th class="n">Pct</th><th class="n">PF</th><th class="n">Yrs</th><th class="n">Titles</th>'
             f'</tr></thead><tbody>{at_rows}</tbody></table></div></div></section>'
             f'<section class="section"><h3>Most lopsided trades ever <small>Points started by what each side received</small></h3>'
             f'<div class="tw">{tr_html}</div></section></div>'
             f'<footer><a href="index.html">All editions</a><span></span></footer>')
    return page(f"{paper}: league history", body)


H2H_JS = """<script>
(function(){
  var D=JSON.parse(document.getElementById('h2h-data').textContent);
  var A=document.getElementById('h2h-a'),B=document.getElementById('h2h-b'),O=document.getElementById('h2h-out');
  function el(tag,txt,cls){var x=document.createElement(tag);if(txt!=null)x.textContent=txt;if(cls)x.className=cls;return x;}
  function run(){
    var a=+A.value,b=+B.value;O.innerHTML='';
    if(a===b){O.appendChild(el('p','Pick two different teams.','note'));return;}
    var wa=0,wb=0,t=0,m=0,rows=[];
    D.games.forEach(function(g){
      var pa,pb;
      if(g[2]===a&&g[3]===b){pa=g[4];pb=g[5];}else if(g[2]===b&&g[3]===a){pa=g[5];pb=g[4];}else return;
      if(pa>pb)wa++;else if(pb>pa)wb++;else t++;
      m+=Math.abs(pa-pb);rows.push([g[0],g[1],pa,pb,g[6]]);
    });
    var na=D.teams[a],nb=D.teams[b];
    if(!rows.length){O.appendChild(el('p',na+' and '+nb+' have never played.','h2h-sum'));return;}
    var rec=Math.max(wa,wb)+'-'+Math.min(wa,wb)+(t?'-'+t:'');
    O.appendChild(el('p',wa===wb?'All square at '+rec:(wa>wb?na:nb)+' leads '+rec,'h2h-sum'));
    O.appendChild(el('p',rows.length+' meeting'+(rows.length>1?'s':'')+', average margin '+(m/rows.length).toFixed(2)+' points.','note'));
    var tb=el('table'),hd=el('thead'),tr=el('tr');
    ['Season','Week',na,nb,''].forEach(function(h,i){var th=el('th',h);if(i>1&&i<4)th.className='n';tr.appendChild(th);});
    hd.appendChild(tr);tb.appendChild(hd);var body=el('tbody');
    rows.reverse().forEach(function(r){
      var row=el('tr');row.appendChild(el('td',String(r[0])));row.appendChild(el('td',String(r[1])));
      var c1=el('td',r[2].toFixed(2),'n'+(r[2]>r[3]?' pos':''));var c2=el('td',r[3].toFixed(2),'n'+(r[3]>r[2]?' pos':''));
      row.appendChild(c1);row.appendChild(c2);row.appendChild(el('td',r[4]?'Playoffs':'','note'));body.appendChild(row);
    });
    tb.appendChild(body);O.appendChild(tb);
  }
  A.addEventListener('change',run);B.addEventListener('change',run);run();
})();
</script>"""


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
    body += ('<p class="histlink"><a href="history.html">League history: champions, all-time records, '
             'and the head-to-head lookup</a></p>')
    body += f'<ul class="archive" style="padding:0;margin:0">{"".join(items)}</ul>'
    (out_dir / "index.html").write_text(page(f"{paper}: archive", body), encoding="utf-8")
    (out_dir / "history.html").write_text(render_history(arc, paper), encoding="utf-8")


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
    print("Fetching NFL player stats and projections...")
    sync_stats(arc["seasons"][season], season, week, data_dir)
    if week >= max(int(k) for k in arc["seasons"][season]["weeks"]):
        record_injuries(arc, arc["seasons"][season], season, week)
    paper = PAPER_NAME or f"The {arc['seasons'][season]['name']} Gazette"
    facts = compute_facts(arc, season, week, paper)
    written = template_write(facts)
    key = os.environ.get("ANTHROPIC_API_KEY")
    used_ai = False
    if key and not args.no_ai:
        print("Claude is writing this week's edition...")
        ai = ai_write(facts, MODEL, key)
        used_ai = isinstance(ai, dict)
        if used_ai:
            ai["_candidates"] = facts.get("photo_candidates", [])
            ai["_games"] = facts.get("games", [])
        written = merge_written(written, ai)

    arc.setdefault("editions", {})[f"{season}-{week}"] = {
        "season": season, "week": week, "facts": facts, "written": written,
        "generated_at": datetime.now().isoformat(timespec="seconds"), "ai": used_ai}
    arc_path.write_text(json.dumps(arc), encoding="utf-8")
    render_all(arc, out_dir, paper)
    print(f"Done: {(out_dir / fname(season, week)).resolve()}")


if __name__ == "__main__":
    main()
