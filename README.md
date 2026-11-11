# WijnKoop

_koop wijn goedkoop_

A private wine value finder that runs on a QNAP NAS. It reads wine prices from Dutch shops
(phase 1: Gall & Gall), stores them in one SQLite file and shows them in a filterable web screen
(Datasette) at `http://<nas-address>:8001`.

Design: see the "WijnKoop – app design" document.

## How it behaves
- Reads only pages that the shop's robots.txt allows, via the product sitemap.
- Waits at least 5 seconds between pages (longer if the shop asks for it).
- Runs every Monday at 01:00 (change with `WIJNKOOP_RUN_WEEKDAY` / `WIJNKOOP_RUN_HOUR`).
- Personal, non-commercial use only.

## Commands (run inside the container)
| Command | What it does |
| --- | --- |
| `python -m wijnkoop check --count 3` | Reads 3 wine pages, prints what it understood, saves the raw pages in `/data/samples` |
| `python -m wijnkoop collect --limit 20` | Reads the first 20 wines into the database |
| `python -m wijnkoop collect` | Reads all wines now (takes hours) |

## Development
```
pip install -r requirements.txt pytest
pytest -q
```
Pushing to `main` builds the image `ghcr.io/<owner>/wijnkoop:latest` with GitHub Actions.
