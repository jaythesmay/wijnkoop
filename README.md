# WijnKoop

_koop wijn goedkoop_

A private wine value finder that runs on a QNAP NAS. It reads wine prices from Dutch shops
(Gall & Gall so far), combines them with award and critic scores, stores everything in one SQLite
file and shows it at `http://<nas-address>:8001`.

Design: see the "WijnKoop – app design" document.

## Screens
Made for a phone, in the shop as well as at home:

| Page | What it is for |
| --- | --- |
| `/` Home | Gold-or-better wines with the best price for their score, deals on award winners, quick finds |
| `/find` | Filters: price per bottle, colour, country, grape, minimum score, gold or better, deals |
| `/search?q=` | Forgiving search on wine, producer and grape (spelling does not need to be exact) |
| `/wine/<id>` | One wine: score, awards, shops, price history, other vintages, better value nearby |
| `/week` | What changed in the last 7 days (the same as the Wednesday e-mail) |
| `/watchlist` | Wines you follow; their changes come first in the e-mail |
| `/order` | Order list per shop with promotions, delivery costs and what is missing for free delivery |
| `/how` | How the score, the price-for-quality label and deals are worked out |
| `/wijnkoop` | The expert tables and saved queries (Datasette) |

The pages are a Datasette plugin (`datasette/plugins/wijnkoop_ui.py`) with templates in
`datasette/templates/`; the data behind them is in `wijnkoop/ui.py`.

**Price for quality.** WijnKoop fits `ln(price) = c + d × score` on all wines with an award and a
price (one point per wine), so it knows what a score usually costs. Until 30 such wines are known it
uses 35% more per point and €12 for a score of 89. The label compares that usual price with the
price per bottle at 6: Top buy (pay two thirds or less), Good buy, Fair, Pricey for its score.

**Away from home.** Use Tailscale (free) on the NAS and the phone instead of opening a router
port: install Tailscale from the QNAP App Center and on the phone, sign in with the same account,
and open `http://<nas tailscale name>:8001`.

## How it behaves
- Reads only pages that the shop's robots.txt allows, via the product sitemap.
- Waits at least 5 seconds between pages (longer if the shop asks for it).
- Runs every Monday at 01:00 (change with `WIJNKOOP_RUN_WEEKDAY` / `WIJNKOOP_RUN_HOUR`).
- Personal, non-commercial use only.
- Keeps every vintage of a shop page apart: a new vintage on the same page starts a new offer.
- Logs every change (new wine, price, promotion, stock, vintage, award, gone) in `event`;
  the "Weekly overview" page shows the last 7 days.
- Scores: awards and critic points (silver and up) combined with competition weights
  (`wijnkoop/reference.py`), value against price, and a deal rule based on the 90-day
  median price for an order of 6 bottles (`wijnkoop/scores.py`).
- Sends the weekly overview by e-mail on Wednesday at 08:00 (later the same week if the NAS was off).
- Older databases are upgraded automatically when the container starts.
- Award results: IWC (with points) and Concours Mondial are read automatically, weekly in
  the award seasons (March, late May to June, September, late November) and monthly otherwise.
  Decanter and IWSC are only known from what shops print, because their sites forbid or block robots.

## Commands (run inside the container)
| Command | What it does |
| --- | --- |
| `python -m wijnkoop check --count 3` | Reads 3 wine pages, prints what it understood, saves the raw pages in `/data/samples` |
| `python -m wijnkoop collect --limit 20` | Reads the first 20 wines into the database |
| `python -m wijnkoop collect` | Reads all wines now (takes hours) |
| `python -m wijnkoop awards` | Reads IWC and Concours Mondial results now and links them to the shop wines |
| `python -m wijnkoop awards --match-only` | Only redoes the linking (after changing the matching rules) |
| `python -m wijnkoop mail-setup` | Sets up the weekly e-mail (Gmail app password, stored only in `/data/mail.ini`) and sends a test |
| `python -m wijnkoop mail` | Sends the weekly overview e-mail now |
| `python -m wijnkoop scores` | Recomputes scores, value and deals (also done after every run) |

## Development
```
pip install -r requirements.txt pytest
pytest -q
```
Pushing to `main` builds the image `ghcr.io/<owner>/wijnkoop:latest` with GitHub Actions.
