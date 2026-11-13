# WijnKoop

_koop wijn goedkoop_

A private wine value finder that runs on a QNAP NAS. It reads wine prices from Dutch shops
(Gall & Gall, Vindict, Henri Bloem and Okhuysen so far), combines them with award and critic scores, stores everything in one SQLite
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
| `/awards` | Every competition and critic, with the wines that won silver or gold |
| `/upload` | Load an official results file (any competition, any format) to add its awards and check the shops' claims |
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
- Shops: Gall & Gall, Henri Bloem and Okhuysen (5 s between pages) and Vindict (10 s, as its
  robots.txt asks). The same wine
  in two shops is recognised (same producer, nearly the same name) so their prices are compared;
  doubtful pairs are listed under "Same wine in two shops?". Vindict also shows press ratings
  (Perswijn out of 20, De Grote Hamersma out of 10), often of an older vintage: those count in the
  track record of the wine, not in the score of the vintage on sale. Okhuysen shows guide ratings
  (Suckling, Parker, ...). Henri Bloem's price for 6 bottles counts as a deal; Okhuysen's
  pre-sale wines ("Voorverkoop") count as not in stock.
- Wijnvoordeel and Wijnbeurs are not read: their prices only load through JavaScript.
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
- Results files you load yourself (`/upload` or `import-results`): a saved web page, Excel, CSV, PDF,
  JSON or text, from any competition or critic. WijnKoop finds the columns from their header or their
  contents (or, for plain text, from headings such as "GOLD" or a medal line under each wine), takes the
  competition and year from the file name or the title, links the wines and lists per wine whether the
  shop's claim is confirmed, different, or missing from the official list. These awards count like the
  others; deleting the upload removes them again. Scanned PDFs (pictures of text) cannot be read.

## Commands (run inside the container)
| Command | What it does |
| --- | --- |
| `python -m wijnkoop check --count 3` | Reads 3 wine pages, prints what it understood, saves the raw pages in `/data/samples` |
| `python -m wijnkoop collect --limit 20` | Reads the first 20 wines into the database |
| `python -m wijnkoop collect` | Reads all wines of every shop now (takes hours) |
| `python -m wijnkoop collect --shop vindict` | Reads one shop now (`gall`, `vindict`, `henribloem` or `okhuysen`) |
| `python -m wijnkoop check --shop vindict --count 3` | Reads 3 pages of one shop and prints what it understood |
| `python -m wijnkoop awards` | Reads IWC and Concours Mondial results now and links them to the shop wines |
| `python -m wijnkoop awards --match-only` | Only redoes the linking (after changing the matching rules) |
| `python -m wijnkoop import-results results.xlsx` | Reads an official results file you downloaded (`--competition DWWA --year 2025` if the file does not say) |
| `python -m wijnkoop mail-setup` | Sets up the weekly e-mail (Gmail app password, stored only in `/data/mail.ini`) and sends a test |
| `python -m wijnkoop vivino-setup` | Saves a Brave Search API key (only in `/data/brave.ini`) for the Vivino test; `--forget` deletes the key and every stored Vivino rating |
| `python -m wijnkoop vivino-test --count 10` | Looks up the Vivino rating of the 10 best-scored wines via Brave (never opens vivino.com; at most 900 searches a month) |
| `python -m wijnkoop mail` | Sends the weekly overview e-mail now |
| `python -m wijnkoop scores` | Recomputes scores, value and deals (also done after every run) |

## Development
```
pip install -r requirements.txt pytest
pytest -q
```
Pushing to `main` builds the image `ghcr.io/<owner>/wijnkoop:latest` with GitHub Actions.
