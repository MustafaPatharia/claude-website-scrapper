# claude-website-scrapper

Small Python toolkit for scraping a website into structured JSON + images, used
to feed a from-scratch rebuild.

## Scripts

### `scraper.py` — crawl a site into `output/`
Breadth-first crawls a single site, extracting per-page `title`, `meta`, and
`sections[]` (each with headings, images, alt text, and a plain-language
description), plus deduped images (extension sniffed from magic bytes).

```bash
python scraper.py https://example.com --max-pages 200 --delay 0.5
```

| arg | default | meaning |
|-----|---------|---------|
| `url` | — | site to crawl (required) |
| `--max-pages` | 200 | crawl cap |
| `--delay` | 0.5 | seconds between requests (be polite) |

Writes `output/pages/*.json`, `output/images/`, and `output/index.json`.

### `build_plan.py` — rebuild allocator (project-specific)
Combines a video catalog + the scraped section metadata + images into one
deterministic rebuild plan (each video used at most once, every image placed,
animation varied per section). Emits `_rebuild-plan.json` + `.md`. Expects the
video catalog + `output/` to be present — it's tailored to a specific rebuild,
kept here as a reference example.

## Setup

```bash
python3 -m venv .scraper-venv
source .scraper-venv/bin/activate
pip install -r requirements.txt
```

`output/`, generated plans, and the venv are gitignored — regenerate by running
the scripts.
