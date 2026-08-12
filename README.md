# The Blame Spectrum — Live Pitcher WAR

Every pitcher WAR is a decision about who owns the runs. This site puts that
decision on a slider: drag from **FIP-based** (the pitcher only controls
strikeouts, walks, and homers — fWAR's philosophy) to **RA9-based** (the
pitcher owns every run that scored — bWAR's philosophy) and watch this
season's leaderboard reshuffle in real time.

Official MLB season stats, refreshed every morning by a GitHub Action.
No server, no build step, no API keys — a static page plus one JSON file.

## How it works

```
.github/workflows/update.yml   # cron, every morning at ~9:15 AM CT
        │
        ▼
src/build_feed.py              # pulls official season pitching lines from
        │                      # the MLB Stats API, computes both WAR poles
        ▼
data/war_spectrum.json         # committed back to the repo
        │
        ▼
index.html                     # static page reads the JSON same-origin
```

The math trick that keeps this simple: WAR is *linear* in the FIP/RA9 blend,
so any point on the spectrum is `lerp(war_fip, war_ra9, λ)`. The feed ships
two numbers per pitcher, every pitcher is a straight line on the chart, and
crossings between pitchers are exact.

## Deploying

**GitHub Pages** (zero config):
1. Push this repo to GitHub (suggested name: `war-spectrum`).
2. Settings → Pages → Deploy from a branch → `main`, `/ (root)` → Save.
3. Actions tab → "Update data" → Run workflow (generates the first
   `data/war_spectrum.json`; after this it runs itself every morning).
4. Your link: `https://<username>.github.io/war-spectrum/` — add a custom
   domain in the same Pages settings screen if you want one.

**Vercel** (nicer default URL, same repo):
1. Do steps 1 and 3 above, then vercel.com → Add New Project → import the
   repo → Framework preset "Other", no build command, output directory
   `./` → Deploy.
2. Your link: `https://war-spectrum.vercel.app` (or a custom domain).
3. Each morning's data commit triggers an automatic redeploy, so the site
   stays current with no extra wiring.

Both can run at once — they're just two front doors to the same repo.

## Method (and honest caveats)

Simplified two-pole WAR, computed per pitcher from official season lines:

- `FIP = (13·HR + 3·(BB+HBP) − 2·K)/IP + cFIP`, put on the RA9 scale
- Replacement level blended by role: .380 win% (SP) / .470 (RP)
- Runs-per-win ≈ `1.5 × lgRA9 + 3`; league constants from everyone who has
  thrown a pitch this season
- Traded pitchers are consolidated so nobody is double-counted

The poles approximate each site's *philosophy*, not their published numbers:
no park factors, no league adjustment, no infield flies in FIP, static
runs-per-win, and no team-defense adjustment on the RA9 pole (which real
bWAR applies).

## Data source

MLB Stats API season pitching splits. Use of MLB data is subject to MLB's
terms of service.
