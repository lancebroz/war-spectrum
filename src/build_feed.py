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
# Statcast fielding values spread narrower than the licensed DRS that bWAR
# uses. Set DEF_SCALE above 1.0 (try 1.4-1.6) to approximate DRS's wider
# spread when benchmarking the RA9 pole against Baseball-Reference.
DEF_SCALE = 1.0

STATS_URL = (
    "https://statsapi.mlb.com/api/v1/stats"
    "?stats=season&group=pitching&season={season}&sportId=1"
    "&playerPool=ALL&limit=3000"
)

# Candidate Savant team-defense CSV endpoints, best scope first: Fielding Run
# Value (range + arms + catcher, already in runs) is much closer to the DRS
# that bWAR uses than bare OAA. Parsing is column-name driven, so any of these
# shapes works; first parseable CSV wins.
# Confirmed-real endpoints (2026): the FRV leaderboard CSV is player-level
# with columns name,id,total_runs,... and no team column, so we aggregate to
# teams with the Stats API roster map. OAA team CSV remains the fallback.
FRV_CSV_URL = "https://baseballsavant.mlb.com/leaderboard/fielding-run-value?csv=true"
ROSTER_URL = "https://statsapi.mlb.com/api/v1/sports/1/players?season={s}"
OAA_TEAM_URL = (
    "https://baseballsavant.mlb.com/leaderboard/outs_above_average"
    "?type=Fielding_Team&startYear={s}&endYear={s}&split=no&team=&range=year"
    "&min=q&pos=&roles=&viz=hide&csv=true")

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

# Official published WAR sources. B-Ref's daily WAR archive is a public,
# daily-updated data file; FanGraphs' leaders API returns leaderboard JSON.
# Both are attributed on the site. If either is unreachable on a given
# morning, that pole falls back to our in-house approximation and the feed
# says so.
BREF_WAR_URL = "https://www.baseball-reference.com/data/war_daily_pitch.txt"
FG_API_URL = (
    "https://www.fangraphs.com/api/leaders/major-league/data"
    "?age=&pos=all&stats=pit&lg=all&qual=0&season={s}&season1={s}"
    "&ind=0&month=0&pageitems=3000&pagenum=1&type=8")

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


def fetch_roster_team_map(season, roster_json=None):
    """player_id -> current team_id, from the Stats API player directory."""
    if roster_json:
        payload = json.loads(Path(roster_json).read_text())
    else:
        import requests
        resp = requests.get(ROSTER_URL.format(s=season), timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    out = {}
    for person in payload.get("people") or []:
        pid = person.get("id")
        tid = (person.get("currentTeam") or {}).get("id")
        if pid is not None and tid is not None:
            out[pid] = tid
    return out


def fetch_team_defense(season, def_csv=None, roster_json=None):
    """
    Team defensive runs above average, keyed by MLB team id.

    Primary: Savant's Fielding Run Value CSV (player-level; range + arms +
    catcher defense — much closer to DRS's scope than bare OAA), aggregated
    to teams via the Stats API roster map. A traded fielder's season FRV is
    credited to his current team — a small, documented approximation.
    Fallback: Savant's team OAA CSV at RUNS_PER_OUT runs per out.
    Returns (runs_by_team_id, source_label); ({}, "unavailable") on failure.
    Values are scaled by DEF_SCALE.
    """
    def _get(url):
        import requests
        try:
            resp = requests.get(url, timeout=30,
                                headers={"User-Agent": "war-spectrum/1.0"})
            return resp.text if resp.ok and "," in resp.text else None
        except requests.RequestException:
            return None

    # --- Primary: FRV player CSV -> team aggregation ---
    frv_text = Path(def_csv).read_text() if def_csv else _get(FRV_CSV_URL)
    if frv_text:
        try:
            rows = list(csv.DictReader(io.StringIO(frv_text)))
            cols = {c.lower().strip().lstrip("\ufeff"): c for c in (rows[0].keys() if rows else [])}
            if "id" in cols and "total_runs" in cols:
                parsed = []
                for row in rows:
                    try:
                        parsed.append((int(float(row[cols["id"]])),
                                       float(row[cols["total_runs"]])))
                    except (TypeError, ValueError, KeyError):
                        continue
                if parsed and all(100 <= pid <= 160 for pid, _ in parsed):
                    # Already team-level (ids are MLBAM team ids)
                    out = {pid: runs * DEF_SCALE for pid, runs in parsed}
                    _report("FRV team CSV", out)
                    return out, "FRV (team)"
                if parsed:
                    try:
                        team_of = fetch_roster_team_map(season, roster_json)
                    except Exception:
                        team_of = {}
                    if team_of:
                        out = {}
                        matched = 0
                        for pid, runs in parsed:
                            tid = team_of.get(pid)
                            if tid is not None:
                                out[tid] = out.get(tid, 0.0) + runs * DEF_SCALE
                                matched += 1
                        if out and matched >= len(parsed) * 0.7:
                            _report(f"FRV players ({matched}/{len(parsed)} mapped)", out)
                            return out, "FRV (player-aggregated)"
        except csv.Error:
            pass

    # --- Fallback: team OAA ---
    oaa_text = _get(OAA_TEAM_URL.format(s=season)) if not def_csv else None
    if oaa_text:
        try:
            rows = list(csv.DictReader(io.StringIO(oaa_text)))
            cols = {c.lower().strip().lstrip("\ufeff"): c for c in (rows[0].keys() if rows else [])}
            id_col = next((cols[c] for c in
                           ("entity_id", "team_id", "id", "fielder_id") if c in cols), None)
            oaa_col = next((cols[c] for c in
                            ("outs_above_average", "oaa") if c in cols), None)
            if id_col and oaa_col:
                out = {}
                for row in rows:
                    try:
                        out[int(float(row[id_col]))] = \
                            float(row[oaa_col]) * RUNS_PER_OUT * DEF_SCALE
                    except (TypeError, ValueError, KeyError):
                        continue
                if out:
                    _report("OAA team CSV", out)
                    return out, "OAA"
        except csv.Error:
            pass

    return {}, "unavailable"


def _report(label, runs_by_team):
    spread = sorted(runs_by_team.values())
    print(f"defense source: {label} | teams: {len(runs_by_team)} | "
          f"spread {spread[0]:+.0f} to {spread[-1]:+.0f} runs")


def fetch_official_bwar(season, bref_file=None):
    """mlb_ID -> season bWAR (stints summed) from B-Ref's daily WAR archive."""
    if bref_file:
        text = Path(bref_file).read_text()
    else:
        import requests
        resp = requests.get(BREF_WAR_URL, timeout=60,
                            headers={"User-Agent": "war-spectrum/1.0"})
        resp.raise_for_status()
        text = resp.text
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            if int(row.get("year_ID", 0)) != season:
                continue
            pid = int(float(row["mlb_ID"]))
            out[pid] = out.get(pid, 0.0) + float(row["WAR"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def fetch_official_fwar(season, fg_file=None):
    """MLBAMID -> season fWAR, from the FanGraphs leaders API (JSON) or a
    saved leaderboard CSV export (both shapes accepted)."""
    if fg_file:
        text = Path(fg_file).read_text()
    else:
        import requests
        resp = requests.get(FG_API_URL.format(s=season), timeout=60,
                            headers={"User-Agent": "war-spectrum/1.0"})
        resp.raise_for_status()
        text = resp.text

    out = {}
    stripped = text.lstrip("\ufeff \n")
    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(stripped)
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        for row in rows or []:
            pid = row.get("xMLBAMID") or row.get("MLBAMID") or row.get("mlbamid")
            war = row.get("WAR")
            try:
                if pid is not None and war is not None:
                    out[int(pid)] = float(war)
            except (TypeError, ValueError):
                continue
    else:
        for row in csv.DictReader(io.StringIO(stripped)):
            cols = {c.strip().lstrip("\ufeff"): c for c in row}
            try:
                out[int(float(row[cols["MLBAMID"]]))] = float(row[cols["WAR"]])
            except (TypeError, ValueError, KeyError):
                continue
    return out


def build_feed(lines, season, team_def_runs, def_source="unavailable",
               fwar_map=None, bwar_map=None):
    fwar_map = fwar_map or {}
    bwar_map = bwar_map or {}
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
            "war_fip": round(fwar_map.get(ln["id"], war_of(fipr9)), 3),
            "war_ra9": round(war_of(ra9), 3),
            "war_ra9_adj": round(bwar_map.get(ln["id"], war_of(ra9_adj)), 3),
            "war_fip_calc": round(war_of(fipr9), 3),
            "war_ra9_calc": round(war_of(ra9_adj), 3),
            "src": ("official" if ln["id"] in fwar_map else "calc") + "/" +
                   ("official" if ln["id"] in bwar_map else "calc"),
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
            "fwar_source": (
                f"official FanGraphs ({len(fwar_map)} pitchers)" if fwar_map
                else "unavailable — in-house FIP-pole approximation this run"
            ),
            "bwar_source": (
                f"official Baseball-Reference ({len(bwar_map)} pitchers)" if bwar_map
                else "unavailable — in-house RA9-pole approximation this run"
            ),
            "def_adjustment": (
                f"Statcast team defense ({def_source}), allocated by BIP share"
                + (f", scaled x{DEF_SCALE}" if DEF_SCALE != 1.0 else "")
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
    ap.add_argument("--bref-file", type=Path, default=None,
                    help="Read a saved war_daily_pitch.txt instead of fetching (testing)")
    ap.add_argument("--fg-file", type=Path, default=None,
                    help="Read a saved FanGraphs leaderboard CSV/JSON instead of fetching (testing)")
    ap.add_argument("--roster-json", type=Path, default=None,
                    help="Read a saved Stats API player directory instead of fetching (testing)")
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
    team_def_runs, def_source = fetch_team_defense(args.season, def_csv=args.def_csv, roster_json=args.roster_json)
    if not team_def_runs:
        print("WARNING: team defense data unavailable; RA9 pole unadjusted.", file=sys.stderr)
    try:
        fwar_map = fetch_official_fwar(args.season, fg_file=args.fg_file)
        print(f"official fWAR loaded: {len(fwar_map)} pitchers")
    except Exception as exc:
        print(f"WARNING: official fWAR unavailable ({exc}); using in-house FIP pole.",
              file=sys.stderr)
        fwar_map = {}
    try:
        bwar_map = fetch_official_bwar(args.season, bref_file=args.bref_file)
        print(f"official bWAR loaded: {len(bwar_map)} pitchers")
    except Exception as exc:
        print(f"WARNING: official bWAR unavailable ({exc}); using in-house RA9 pole.",
              file=sys.stderr)
        bwar_map = {}
    feed = build_feed(lines, args.season, team_def_runs, def_source, fwar_map, bwar_map)

    body = json.dumps(feed, separators=(",", ":"))
    for out in OUT_PATHS:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)
        print(f"Wrote {out.relative_to(REPO_ROOT)} "
              f"({len(feed['pitchers'])} pitchers, {len(body):,} bytes)")


if __name__ == "__main__":
    main()
