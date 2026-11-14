# WijnKoop

_koop wijn goedkoop_

A private wine value finder that runs on a QNAP NAS. It reads wine prices from Dutch shops
(Gall & Gall, Vindict, Henri Bloem, Okhuysen, Wijnvoordeel, Wijnbeurs, Grand Cru Wijnen,
Grape District, Vino.com, Vanouds de Zwaan, Vinio, Colaris, Wijny, Alex Storms and Rootring Wijnen so far), combines them with award and critic scores, stores everything in one SQLite
file and shows it at `http://<nas-address>:8001`.

Design: see the "WijnKoop – app design" document.

## Screens
Made for a phone, in the shop as well as at home:

| Page | What it is for |
| --- | --- |
| `/` Home | Gold-or-better wines with the best price for their score, deals on award winners, quick finds |
| `/update` | Read shops now: when each shop was last read, a button per shop and one for every shop that needs it, the runs going on, and a stop button |
| `/find` | Filters: price per bottle (per 75 cl), colour, country, grape, minimum score, gold or better, deals |
| `/search?q=` | Forgiving search on wine, producer and grape (spelling does not need to be exact) |
| `/wine/<id>` | One wine: score, awards, shops, price history, other vintages, better value nearby |
| `/week` | What changed in the last 7 days (the same as the Wednesday e-mail) |
| `/watchlist` | Wines you follow; their changes come first in the e-mail |
| `/cellar` | Your cellar: what you bought, how many bottles, until when to drink it (drink-soon first) |
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
Every price is per 75 cl, so a half bottle or a magnum compares fairly; the size comes from the
shop, or from the name or address ("37,5cl", "halve fles", "Magnum") in `wijnkoop/bottles.py`.

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
  pre-sale wines ("Voorverkoop") count as not in stock. Grand Cru Wijnen shows Parker, Suckling and
  Vinous scores (about 5,000 pages, 7 hours); Grape District shows Hamersma and other guide ratings.
  Vino.com (Italian, delivers in NL; about 6,800 pages, 9 to 10 hours) shows Suckling, Parker,
  Falstaff and other critic scores, often of another vintage; its own "Vino.com" score is left out.
  Its "Laagste prijs" (lowest price of the last 30 days) counts as the normal price of a reduced wine.
- Vanouds de Zwaan (about 2,900 products) and Vinio (about 180) are Shopify shops: WijnKoop reads the
  product list Shopify offers (`/products.json`, 250 wines per request, so a run takes a few minutes); a wine missing there is read from its own small product data. Vinio's descriptions name critic
  scores (Parker, Suckling, Falstaff). Colaris (about 1,400 pages) asks for 20 s between pages, so a run
  takes about 8 hours; it shows Parker and other critic scores. Wijny is Bon Import's webshop (about
  4,000 pages including spirits and beer, which are skipped). Alex Storms (about 480 wines) shows
  Hamersma and Onno Kleyn ratings. Rootring Wijnen (about 4,000 wines) is read through its colour pages.
- Not read: Brander Wines and Decantalo refuse automated visitors, bonimport.nl asks robots to stay
  away (its shop Wijny is read), Beste Spaanse Wijn has no webshop, and Alleen Italiaanse Producten
  has about 22,000 wines (a run would take more than 30 hours).
- Wijnvoordeel and Wijnbeurs show their prices only after the page's JavaScript ran. That JavaScript asks
  the shop's product service (Adobe Commerce GraphQL, allowed by robots.txt), and WijnKoop asks the same
  service directly: the whole shop in 4 to 6 requests of 100 products, 20 s apart. No headless browser is
  needed any more. Their medals come from the medal images; packages and cases are skipped; pre-orders count
  as not in stock.
- Reads only pages that changed: most shops date every product in their sitemap (`<lastmod>`); a wine read
  after its last change is not opened again and counts as seen with the price it had. Every shop is read in
  full once every 4 weeks anyway (`wijnkoop/incremental.py`).
- Notices a shop that changed its site: when more than 30% of the pages of a run have no price or cannot be
  read, Read shops now and the weekly e-mail say so (`wijnkoop/health.py`).
- Runs every Monday at 01:00 (change with `WIJNKOOP_RUN_WEEKDAY` / `WIJNKOOP_RUN_HOUR`).
- Personal, non-commercial use only.
- Keeps every vintage of a shop page apart: a new vintage on the same page starts a new offer.
- Logs every change (new wine, price, promotion, stock, vintage, award, gone) in `event`;
  the "Weekly overview" page shows the last 7 days.
- Scores: awards and critic points (silver and up) combined with competition weights
  (`wijnkoop/reference.py`), value against price (per bottle at 6 with the promotion, or the Gall
  Premium price when lower), and a deal rule based on the 90-day
  median price for an order of 6 bottles (`wijnkoop/scores.py`).
- Sends the weekly overview by e-mail on Wednesday at 08:00 (later the same week if the NAS was off).
- Older databases are upgraded automatically when the container starts.
- Makes a copy of the database every night at 04:00 in `/data/backups` (`WIJNKOOP_BACKUP_HOUR`): the last
  7 days and one a week for 4 weeks before that. To go back, stop the container and put a copy in place of
  `/data/wijnkoop.db`.
- Works out scores, value and deals once after the shops are read (at most an hour after new prices
  come in while other shops are still being read), not after every shop.
- Yours, on every wine page: your own rating (1 to 5 stars) and note, a target price, and Add to cellar.
  When a watched wine's cheapest offer in stock reaches your target, the watch list and the Wednesday e-mail
  say so, and with `WIJNKOOP_NTFY_URL` set (a free ntfy.sh topic only you know, e.g.
  `https://ntfy.sh/wijnkoop-k7x2q9`; subscribe to it in the ntfy app) your phone gets a push message.
- Uploads are worked on beside the app, not on its single write thread, so the buttons stay quick meanwhile.
- Keeps the price history as changes only (a line when price, promotion or stock changed); older weekly lines
  are folded together once after the update, which makes the database many times smaller.
- The search box uses a search index (SQLite FTS5 with three-letter pieces), so a typo still finds the wine.
- Upgrades the database only once after an update, not on every connection.
- Runs as the plain user `wijnkoop`, not as root (the data folder is handed to that user at start;
  `WIJNKOOP_RUN_AS_ROOT=1` if the NAS does not allow that). Uploads are at most 25 MB
  (`WIJNKOOP_MAX_UPLOAD_MB`). With `WIJNKOOP_PASSWORD` set, the app asks for a password.
- The log is also kept in `/data/logs/wijnkoop.log` (5 files of 1 MB at most).
- Every image is also tagged with its date and code (`ghcr.io/<owner>/wijnkoop:20261114-abc1234`), so you can go
  back to an earlier version in Container Station.
- The container uses at most 2 processor cores and 1.5 GB of memory (`cpus` and `mem_limit` in
  `docker-compose.yml`).
- Award results: IWC (with points) and Concours Mondial are read automatically, weekly in
  the award seasons (March, late May to June, September, late November) and monthly otherwise.
  Decanter and IWSC are only known from what shops print, because their sites forbid or block robots.
- Results files you load yourself (`/upload` or `import-results`): a saved web page, Excel, CSV, PDF,
  JSON or text, from any competition or critic. WijnKoop finds the columns from their header or their
  contents (or, for plain text, from headings such as "GOLD" or a medal line under each wine), takes the
  competition and year from the file name or the title, links the wines and lists per wine whether the
  shop's claim is confirmed, different, or missing from the official list. These awards count like the
  others, and where an official result and a shop's claim cover the same competition and year, the
  official one counts. Deleting the upload removes them again. Scanned PDFs (pictures of text) cannot
  be read. The file itself is kept in `/data/uploads`, and after every shop run the awards that are
  not linked yet are checked against the wines found since, so old results keep finding new wines.

## Commands (run inside the container)
| Command | What it does |
| --- | --- |
| `python -m wijnkoop check --count 3` | Reads 3 wine pages, prints what it understood, saves the raw pages in `/data/samples` |
| `python -m wijnkoop collect --limit 20` | Reads the first 20 wines into the database |
| `python -m wijnkoop collect` | Reads all wines of every shop now (takes hours) |
| `python -m wijnkoop collect --shop vindict` | Reads one shop now (`gall`, `vindict`, `henribloem`, `okhuysen`, `wijnvoordeel`, `wijnbeurs`, `grandcru`, `grapedistrict`, `vinocom`, `vanoudsdezwaan`, `vinio`, `colaris`, `wijny`, `storms` or `rootring`) |
| `python -m wijnkoop collect --shop vindict,vinocom` | Reads these shops, one after the other |
| `python -m wijnkoop collect --new` | Reads the shops never read before |
| `python -m wijnkoop collect --due` | Reads the shops not read in the last two days |
| `python -m wijnkoop check --shop vindict --count 3` | Reads 3 pages of one shop and prints what it understood |
| `python -m wijnkoop awards` | Reads IWC and Concours Mondial results now and links them to the shop wines |
| `python -m wijnkoop awards --match-only` | Only redoes the linking (after changing the matching rules) |
| `python -m wijnkoop import-results results.xlsx` | Reads an official results file you downloaded (`--competition DWWA --year 2025` if the file does not say) |
| `python -m wijnkoop mail-setup` | Sets up the weekly e-mail (Gmail app password, stored only in `/data/mail.ini`) and sends a test |
| `python -m wijnkoop vivino-setup` | Saves a Tavily API key (free account at app.tavily.com: 1,000 searches a month, no credit card; stored only in `/data/tavily.ini`) for the Vivino ratings and tests it on 3 wines; `--forget` deletes the key and every stored Vivino rating |
| `python -m wijnkoop vivino-test --count 10` | Looks up the Vivino rating and price (in euros, when the search result shows one) of the 10 best-scored wines via Tavily search (never opens vivino.com; at most 900 searches a month) |
| `python -m wijnkoop mail` | Sends the weekly overview e-mail now |
| `python -m wijnkoop scores` | Recomputes scores, value and deals (also done after every run) |

## Development
```
pip install -r requirements.txt pytest
pytest -q
```
Pushing to `main` builds the image `ghcr.io/<owner>/wijnkoop:latest` with GitHub Actions.
