#!/usr/bin/env python3
"""
Build the WAR blame-spectrum feed from official season-to-date pitching lines.

Pulls every pitcher's season stats from the MLB Stats API (the same API this
repo's pitch ETL uses), computes the two poles of pitcher WAR:

  - war_fip : fielding-independent pole (FIP-based, fWAR philosophy)
  - war_ra9 : results-based pole (runs-allowed-based, bWAR philosophy)

and writes a JSON feed the front-end interpolates between. Because WAR
is linear in the FIP/RA9 blend, any point on the spectrum is just
lerp(war_fip, war_ra9, lambda) — the front-end never needs to redo the math.

Method (simplified, documented in the feed and on the page):
  FIP    = (13*HR + 3*(BB+HBP) - 2*K) / IP + cFIP, with cFIP from league totals
  FIPR9  = FIP + (lgRA9 - lgERA)                   (put FIP on the RA9 scale)
  RPW    = 1.5 * lgRA9 + 3                          (runs-per-win heuristic)
  repW   = 0.380*(GS/G) + 0.470*(1 - GS/G)          (SP/RP replacement blend)
  repRA9 = lgRA9 * sqrt((1-repW)/repW)              (Pythagorean, exponent 2)
  WAR    = (repRA9 - RA9_or_FIPR9) / RPW * IP/9

Known simplifications vs the real published metrics: no park factors, no
league (AL/NL) adjustment, no infield flies in FIP, static rather than
dynamic runs-per-win, and no team-defense adjustment on the RA9 pole (which
real bWAR applies). The poles approximate each site's philosophy, not their
published numbers.

Output:
  - data/war_spectrum.json   (served same-origin by the static site)

Usage:
  python src/build_feed.py                 # live fetch
  python src/build_feed.py --season 2026
  python src/build_feed.py --input-json f  # offline test fixture
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
CENTRAL_TZ = ZoneInfo("America/Chicago")

DEFAULT_SEASON = 2026
MIN_IP = 20.0  # pitchers below this stay out of the feed (noise + file size)

STATS_URL = (
    "https://statsapi.mlb.com/api/v1/stats"
    "?stats=season&group=pitching&season={season}&sportId=1"
    "&playerPool=ALL&limit=3000"
)

OUT_PATHS = [REPO_ROOT / "data" / "war_spectrum.json"]


def parse_ip(ip_str):
    """Official innings pitched string -> float. '150.2' means 150 and 2/3."""
    if ip_str is None:
        return 0.0
    s = str(ip_str)
    if "." in s:
        whole, frac = s.split(".", 1)
        whole = int(whole or 0)
        frac = int(frac or 0)
    else:
        whole, frac = int(s or 0), 0
    if frac not in (0, 1, 2):
        # Defensive: treat malformed fractions as raw decimal.
        return float(s)
    return whole + frac / 3.0


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fetch_splits(season):
    import requests

    url = STATS_URL.format(season=season)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    stats = payload.get("stats") or []
    if not stats:
        raise RuntimeError("Stats API returned no 'stats' block")
    return stats[0].get("splits") or []


def consolidate(splits):
    """
    One line per pitcher.

    Traded players come back as one split per team stint; the API sometimes
    also includes a season-combined split (identifiable by a missing team or a
    'numTeams' marker). Prefer the combined split when present, otherwise sum
    the stints, so nobody is double-counted.
    """
    by_player = {}
    for sp in splits:
        player = sp.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        by_player.setdefault(pid, []).append(sp)

    lines = []
    for pid, group in by_player.items():
        combined = [
            sp for sp in group
            if "team" not in sp or sp.get("numTeams") is not None
        ]
        chosen = combined[-1:] if combined else group

        name = (chosen[0].get("player") or {}).get("fullName", f"Player {pid}")
        teams = []
        for sp in group:
            abbr = (sp.get("team") or {}).get("abbreviation") or (sp.get("team") or {}).get("name")
            if abbr and abbr not in teams:
                teams.append(abbr)

        totals = {"outs": 0, "k": 0, "bb": 0, "hbp": 0, "hr": 0,
                  "r": 0, "er": 0, "g": 0, "gs": 0}
        for sp in chosen:
            st = sp.get("stat") or {}
            totals["outs"] += round(parse_ip(st.get("inningsPitched")) * 3)
            totals["k"] += _i(st.get("strikeOuts"))
            totals["bb"] += _i(st.get("baseOnBalls"))
            totals["hbp"] += _i(st.get("hitByPitch"))
            totals["hr"] += _i(st.get("homeRuns"))
            totals["r"] += _i(st.get("runs"))
            totals["er"] += _i(st.get("earnedRuns"))
            totals["g"] += _i(st.get("gamesPlayed"))
            totals["gs"] += _i(st.get("gamesStarted"))

        lines.append({
            "id": pid,
            "name": name,
            "team": " / ".join(teams) if teams else "",
            **totals,
        })
    return lines


def ip_display(outs):
    return f"{outs // 3}.{outs % 3}"


def build_feed(lines, season):
    # League constants from everyone who threw a pitch, not just the feed cut.
    lg = {"outs": 0, "k": 0, "bb": 0, "hbp": 0, "hr": 0, "r": 0, "er": 0}
    for ln in lines:
        for key in lg:
            lg[key] += ln[key]
    lg_ip = lg["outs"] / 3.0
    if lg_ip <= 0:
        raise RuntimeError("League innings total is zero — bad or empty input")

    lg_ra9 = lg["r"] * 9.0 / lg_ip
    lg_era = lg["er"] * 9.0 / lg_ip
    cfip = lg_era - (13 * lg["hr"] + 3 * (lg["bb"] + lg["hbp"]) - 2 * lg["k"]) / lg_ip
    rpw = 1.5 * lg_ra9 + 3.0
    fip_to_ra9 = lg_ra9 - lg_era

    pitchers = []
    for ln in lines:
        ip = ln["outs"] / 3.0
        if ip < MIN_IP:
            continue
        fip = (13 * ln["hr"] + 3 * (ln["bb"] + ln["hbp"]) - 2 * ln["k"]) / ip + cfip
        ra9 = ln["r"] * 9.0 / ip
        era = ln["er"] * 9.0 / ip
        fipr9 = fip + fip_to_ra9
        gs_share = (ln["gs"] / ln["g"]) if ln["g"] > 0 else 0.0
        rep_w = 0.380 * gs_share + 0.470 * (1.0 - gs_share)
        rep_ra9 = lg_ra9 * math.sqrt((1.0 - rep_w) / rep_w)
        war_fip = (rep_ra9 - fipr9) / rpw * ip / 9.0
        war_ra9 = (rep_ra9 - ra9) / rpw * ip / 9.0

        pitchers.append({
            "id": ln["id"],
            "name": ln["name"],
            "team": ln["team"],
            "ip": round(ip, 2),
            "ip_display": ip_display(ln["outs"]),
            "g": ln["g"],
            "gs": ln["gs"],
            "k": ln["k"],
            "bb": ln["bb"],
            "hbp": ln["hbp"],
            "hr": ln["hr"],
            "r": ln["r"],
            "er": ln["er"],
            "era": round(era, 2),
            "fip": round(fip, 2),
            "ra9": round(ra9, 2),
            "fipr9": round(fipr9, 3),
            "rep_ra9": round(rep_ra9, 3),
            "war_fip": round(war_fip, 3),
            "war_ra9": round(war_ra9, 3),
        })

    # Sort by midpoint WAR so the feed's natural order is useful on its own.
    pitchers.sort(key=lambda p: (p["war_fip"] + p["war_ra9"]) / 2, reverse=True)

    # Run in the morning, season-to-date stats cover games through yesterday.
    from datetime import timedelta
    data_through = (datetime.now(CENTRAL_TZ).date() - timedelta(days=1)).isoformat()

    return {
        "meta": {
            "season": season,
            "generated_at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds"),
            "data_through": data_through,
            "source": "MLB Stats API season pitching splits",
            "min_ip": MIN_IP,
            "method": (
                "Simplified two-pole WAR: FIP-based pole (~fWAR philosophy) and "
                "RA9-based pole (~bWAR philosophy, without its team-defense "
                "adjustment). No park factors, static runs-per-win. "
                "WAR at any blend = lerp(war_fip, war_ra9, lambda)."
            ),
        },
        "league": {
            "ra9": round(lg_ra9, 3),
            "era": round(lg_era, 3),
            "cfip": round(cfip, 3),
            "rpw": round(rpw, 3),
            "ip": round(lg_ip, 1),
            "hr": lg["hr"],
            "k": lg["k"],
            "bb": lg["bb"],
            "hbp": lg["hbp"],
            "r": lg["r"],
        },
        "pitchers": pitchers,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON)
    ap.add_argument("--input-json", type=Path, default=None,
                    help="Read a saved Stats API response instead of fetching (testing)")
    args = ap.parse_args()

    if args.input_json:
        payload = json.loads(args.input_json.read_text())
        splits = (payload.get("stats") or [{}])[0].get("splits") or []
    else:
        splits = fetch_splits(args.season)

    if not splits:
        print("No pitching splits returned — leaving existing feed untouched.", file=sys.stderr)
        sys.exit(1)

    lines = consolidate(splits)
    feed = build_feed(lines, args.season)

    body = json.dumps(feed, separators=(",", ":"))
    for out in OUT_PATHS:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)
        print(f"Wrote {out.relative_to(REPO_ROOT)} "
              f"({len(feed['pitchers'])} pitchers, {len(body):,} bytes)")


if __name__ == "__main__":
    main()
