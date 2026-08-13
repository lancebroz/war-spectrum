#!/usr/bin/env python3
"""
Build the WAR blame-spectrum feed from official season-to-date pitching lines.

Two WAR poles per pitcher:
  - war_fip     : fielding-independent pole (FIP-based, fWAR philosophy)
  - war_ra9     : results-based pole (runs allowed per 9)
  - war_ra9_adj : RA9 pole with a bWAR-style team-defense adjustment

Defense adjustment (bWAR's mechanism, free data):
  Baseball-Reference shifts each pitcher's runs-allowed context by the quality
  of the defense behind him — team defensive runs apportioned by his share of
  the team's balls in play. They use licensed DRS; we use Statcast team Outs
  Above Average from Baseball Savant, converted at RUNS_PER_OUT runs per out.
  A pitcher whose defense saved him runs gets those runs added back to his
  RA9 before the WAR calc (good defense -> higher expectations -> lower WAR).
  Traded pitchers are allocated per stint. If the Savant fetch fails, the
  adjustment is zero and meta.def_adjustment says "unavailable".

Other method notes (documented in the feed):
  FIP    = (13*HR + 3*(BB+HBP) - 2*K)/IP + cFIP, cFIP from league totals
  FIPR9  = FIP + (lgRA9 - lgERA)
  RPW    = 1.5 * lgRA9 + 3
  repW   = 0.380*(GS/G) + 0.470*(1 - GS/G)
  repRA9 = lgRA9 * sqrt((1-repW)/repW)
  WAR    = (repRA9 - rate) / RPW * IP/9
  No park factors, no league adjustment, static runs-per-win.

Output:
  - data/war_spectrum.json

Usage:
  python src/build_feed.py                    # live fetch
  python src/build_feed.py --season 2026
  python src/build_feed.py --input-json f     # offline stats fixture
  python src/build_feed.py --def-csv f        # offline Savant CSV fixture
"""

import argparse
import csv
import io
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
CENTRAL_TZ = ZoneInfo("America/Chicago")

DEFAULT_SEASON = 2026
MIN_IP = 20.0
RUNS_PER_OUT = 0.8  # Statcast OAA outs -> runs

STATS_URL = (
    "https://statsapi.mlb.com/api/v1/stats"
    "?stats=season&group=pitching&season={season}&sportId=1"
    "&playerPool=ALL&limit=3000"
)

# Candidate Savant team-defense CSV endpoints, best scope first: Fielding Run
# Value (range + arms + catcher, already in runs) is much closer to the DRS
# that bWAR uses than bare OAA. Parsing is column-name driven, so any of these
# shapes works; first parseable CSV wins.
SAVANT_URLS = [
    ("https://baseballsavant.mlb.com/leaderboard/fielding_run_value"
     "?type=team&startYear={s}&endYear={s}&split=no&team=&range=year"
     "&min=q&pos=&roles=&viz=hide&csv=true"),
    ("https://baseballsavant.mlb.com/leaderboard/outs_above_average"
     "?type=Fielding_Team&startYear={s}&endYear={s}&split=no&team=&range=year"
     "&min=q&pos=&roles=&viz=hide&csv=true"),
    ("https://baseballsavant.mlb.com/leaderboard/outs_above_average"
     "?type=team&startYear={s}&endYear={s}&split=no&team=&range=year"
     "&min=q&pos=&roles=&viz=hide&csv=true"),
]

# Pitcher park factors (100 = neutral), keyed by MLBAM team id. Multi-year
# style, like B-Ref's PPFp. Approximate values baked as constants — update
# once a season. Sourced/rounded from published 3-year factors.
PARK_FACTORS = {
    108: 100,  # LAA
    109: 103,  # ARI
    110: 99,   # BAL
    111: 104,  # BOS
    112: 100,  # CHC
    113: 104,  # CIN
    114: 98,   # CLE
    115: 110,  # COL
    116: 98,   # DET
    117: 99,   # HOU
    118: 101,  # KC
    119: 97,   # LAD
    120: 100,  # WSH
    121: 97,   # NYM
    133: 99,   # ATH
    134: 98,   # PIT
    135: 96,   # SD
    136: 96,   # SEA
    137: 98,   # SF
    138: 98,   # STL
    139: 97,   # TB
    140: 101,  # TEX
    141: 100,  # TOR
    142: 99,   # MIN
    143: 102,  # PHI
    144: 100,  # ATL
    145: 101,  # CWS
    146: 97,   # MIA
    147: 100,  # NYY
    158: 99,   # MIL
}

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
        return float(s)
    return whole + frac / 3.0


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def stat_line(st):
    """Extract the counting stats we need from one API stat object."""
    outs = round(parse_ip(st.get("inningsPitched")) * 3)
    line = {
        "outs": outs,
        "k": _i(st.get("strikeOuts")),
        "bb": _i(st.get("baseOnBalls")),
        "hbp": _i(st.get("hitByPitch")),
        "hr": _i(st.get("homeRuns")),
        "r": _i(st.get("runs")),
        "er": _i(st.get("earnedRuns")),
        "h": _i(st.get("hits")),
        "bf": _i(st.get("battersFaced")),
        "g": _i(st.get("gamesPlayed")),
        "gs": _i(st.get("gamesStarted")),
    }
    if line["bf"] <= 0:
        # Approximate batters faced: outs recorded + baserunners allowed.
        line["bf"] = outs + line["h"] + line["bb"] + line["hbp"]
    return line


def bip_of(line):
    """Balls in play against this line: BF - K - BB - HBP - HR (floor 0)."""
    return max(0, line["bf"] - line["k"] - line["bb"] - line["hbp"] - line["hr"])


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
    One line per pitcher, plus per-team stints for defense allocation.

    Traded players come back as one split per team stint; the API sometimes
    also includes a season-combined split (missing team / 'numTeams' marker).
    Totals prefer the combined split, otherwise sum stints. Stints always come
    from the per-team splits so defense can be allocated to the right club.
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
        combined = [sp for sp in group if "team" not in sp or sp.get("numTeams") is not None]
        team_splits = [sp for sp in group if sp not in combined]
        chosen = combined[-1:] if combined else team_splits

        name = (chosen[0].get("player") or {}).get("fullName", f"Player {pid}")
        teams = []
        stints = []  # (team_id, bip)
        for sp in team_splits or group:
            team = sp.get("team") or {}
            abbr = team.get("abbreviation") or team.get("name")
            if abbr and abbr not in teams:
                teams.append(abbr)
            tid = team.get("id")
            if tid is not None:
                stints.append((tid, bip_of(stat_line(sp.get("stat") or {}))))

        totals = {"outs": 0, "k": 0, "bb": 0, "hbp": 0, "hr": 0,
                  "r": 0, "er": 0, "h": 0, "bf": 0, "g": 0, "gs": 0}
        for sp in chosen:
            line = stat_line(sp.get("stat") or {})
            for key in totals:
                totals[key] += line[key]

        lines.append({
            "id": pid,
            "name": name,
            "team": " / ".join(teams) if teams else "",
            "stints": stints,
            **totals,
        })
    return lines


def fetch_team_defense(season, def_csv=None):
    """
    Team defensive runs above average, keyed by MLB team id.

    Source: Baseball Savant team OAA leaderboard CSV (or a local file via
    --def-csv). Column detection is name-driven so minor endpoint changes
    don't break the build. Returns {} on any failure — callers treat that as
    'no adjustment'.
    """
    texts = []
    if def_csv:
        texts.append(Path(def_csv).read_text())
    else:
        import requests
        for url in SAVANT_URLS:
            try:
                resp = requests.get(url.format(s=season), timeout=30,
                                    headers={"User-Agent": "war-spectrum/1.0"})
                if resp.ok and "," in resp.text:
                    texts.append(resp.text)
                    break
            except requests.RequestException:
                continue

    for text in texts:
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
            if not rows:
                continue
            cols = {c.lower().strip(): c for c in rows[0].keys()}
            id_col = next((cols[c] for c in
                           ("entity_id", "team_id", "id", "fielder_id") if c in cols), None)
            runs_col = next((cols[c] for c in
                             ("fielding_runs_prevented", "run_value", "runs_prevented")
                             if c in cols), None)
            oaa_col = next((cols[c] for c in
                            ("outs_above_average", "oaa") if c in cols), None)
            if not id_col or not (runs_col or oaa_col):
                continue
            out = {}
            for row in rows:
                try:
                    tid = int(float(row[id_col]))
                    if runs_col and row.get(runs_col) not in (None, ""):
                        out[tid] = float(row[runs_col])
                    else:
                        out[tid] = float(row[oaa_col]) * RUNS_PER_OUT
                except (TypeError, ValueError, KeyError):
                    continue
            if out:
                return out
        except csv.Error:
            continue
    return {}


def build_feed(lines, season, team_def_runs):
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

    # Team BIP totals from our own data -> defensive runs per ball in play.
    team_bip = {}
    for ln in lines:
        for tid, bip in ln["stints"]:
            team_bip[tid] = team_bip.get(tid, 0) + bip
    def_per_bip = {
        tid: (team_def_runs.get(tid, 0.0) / team_bip[tid]) if team_bip.get(tid) else 0.0
        for tid in team_bip
    }
    def_active = bool(team_def_runs)

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
        # FanGraphs replacement structure: 0.12 wins per 9 for a starter,
        # 0.03 for a reliever (equivalent to .380/.470 replacement win%).
        rep9 = 0.12 * gs_share + 0.03 * (1.0 - gs_share)

        # Park factor: stint-BIP-weighted pitcher park factor (100 = neutral).
        tot_bip = sum(b for _, b in ln["stints"])
        if tot_bip > 0:
            ppf = sum(PARK_FACTORS.get(tid, 100) * b for tid, b in ln["stints"]) / tot_bip
        else:
            ppf = 100.0
        # Expected run context in this pitcher's park(s); both poles are
        # measured against it (both fWAR and bWAR park-adjust). The venue
        # factor is halved toward neutral because half his games are on the
        # road — standard for player-applied park factors.
        baseline = lg_ra9 * ((ppf + 100.0) / 2.0) / 100.0

        # Defensive support behind this pitcher, allocated per stint by BIP.
        def_runs = sum(def_per_bip.get(tid, 0.0) * bip for tid, bip in ln["stints"])
        ra9_adj = ra9 + def_runs * 9.0 / ip

        ip_per_g = min(9.0, max(1.0, ip / ln["g"])) if ln["g"] > 0 else 4.5

        def war_of(rate):
            # FanGraphs' dynamic runs-per-win, exactly: dRPW = (([(18 − IP/G)
            # × lgFIPR9] + [IP/G × pRate]) / 18 + 2) × 1.5, with the park
            # baseline as the league context. The pitcher shapes the run
            # environment of his own games in proportion to his IP/G.
            r = min(max(rate, 0.0), 3.0 * baseline)
            drpw = (((18.0 - ip_per_g) * baseline + ip_per_g * r) / 18.0 + 2.0) * 1.5
            return ((baseline - rate) / drpw + rep9) * ip / 9.0

        pitchers.append({
            "id": ln["id"],
            "name": ln["name"],
            "team": ln["team"],
            "ip": round(ip, 2),
            "ip_display": f"{ln['outs'] // 3}.{ln['outs'] % 3}",
            "g": ln["g"],
            "gs": ln["gs"],
            "k": ln["k"],
            "bb": ln["bb"],
            "hbp": ln["hbp"],
            "hr": ln["hr"],
            "r": ln["r"],
            "er": ln["er"],
            "h": ln["h"],
            "bf": ln["bf"],
            "ppf": round(ppf, 1),
            "era": round(era, 2),
            "fip": round(fip, 2),
            "ra9": round(ra9, 2),
            "fipr9": round(fipr9, 3),
            "def_runs": round(def_runs, 1),
            "ra9_adj": round(ra9_adj, 2),
            "war_fip": round(war_of(fipr9), 3),
            "war_ra9": round(war_of(ra9), 3),
            "war_ra9_adj": round(war_of(ra9_adj), 3),
        })

    pitchers.sort(key=lambda p: (p["war_fip"] + p["war_ra9_adj"]) / 2, reverse=True)

    data_through = (datetime.now(CENTRAL_TZ).date() - timedelta(days=1)).isoformat()

    return {
        "meta": {
            "season": season,
            "generated_at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds"),
            "data_through": data_through,
            "source": "MLB Stats API season pitching splits",
            "min_ip": MIN_IP,
            "def_adjustment": (
                f"Statcast team OAA at {RUNS_PER_OUT} runs/out, allocated by BIP share"
                if def_active else "unavailable — RA9 pole unadjusted this run"
            ),
            "method": (
                "Two-pole WAR: FIP-based pole (~fWAR philosophy) and RA9-based "
                "pole with a bWAR-style team-defense adjustment (war_ra9_adj; "
                "war_ra9 is unadjusted). Both poles are park-adjusted (static "
                "PPF table) and use FanGraphs' exact per-pitcher dynamic "
                "runs-per-win and replacement structure (0.12/0.03 wins per 9, "
                "SP/RP). Not modeled: opponent quality, reliever leverage, "
                "infield flies. WAR at any blend = "
                "lerp(war_fip, war_ra9_adj, lambda)."
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
    ap.add_argument("--def-csv", type=Path, default=None,
                    help="Read a saved Savant team-OAA CSV instead of fetching (testing)")
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
    team_def_runs = fetch_team_defense(args.season, def_csv=args.def_csv)
    if not team_def_runs:
        print("WARNING: team defense data unavailable; RA9 pole unadjusted.", file=sys.stderr)
    feed = build_feed(lines, args.season, team_def_runs)

    body = json.dumps(feed, separators=(",", ":"))
    for out in OUT_PATHS:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)
        print(f"Wrote {out.relative_to(REPO_ROOT)} "
              f"({len(feed['pitchers'])} pitchers, {len(body):,} bytes)")


if __name__ == "__main__":
    main()
