# Property Assistant: DarGlobal and Wasalt

A chatbot that answers property questions from data scraped from darglobal.co.uk and wasalt.sa. Tell it your
requirements (budget, city, sale or rent, bedrooms) and it finds matching properties, explains prices, and shows
them as cards with photos, each linked to its source page. Works in English and Arabic.

- **Live:** https://property-assistant-8mt5.onrender.com (free instance: the first visit after 15 idle minutes takes about a minute to wake)
- **Model:** free models on OpenRouter, tried in order on rate limits (`OPENROUTER_MODELS`)
- **Stack:** Python, FastAPI, SQLite (FTS5), one Docker container on Render

## How it answers

```
question ──▶ agent loop (free OpenRouter model, tool calling, up to 3 rounds)
               │  picks the right lookup for each part of the question:
               ├─ search_listings / listing_stats ──▶ listings table: exact SQL on price, bedrooms, area, city
               ├─ list_projects / get_project ──────▶ projects table: DarGlobal project facts
               ├─ search_docs / get_page ───────────▶ docs (FTS5, BM25): page text, blog, press, descriptions
               └─ photos ───────────────────────────▶ images table: photos mapped to listing/project + room label
             checks every part of the question is covered; if not, searches again
         ──▶ answer with links ──▶ server adds a card per linked property and a gallery per photo request
```

Property questions are mostly **filters** ("3-bed villa in Riyadh under 2M SAR"). Vector similarity cannot apply
"under 2M"; SQL can, exactly. So prices, bedrooms and areas are typed columns, and the model calls fixed filter
tools (not free-form SQL: safer, and more reliable on free models). Text questions ("what amenities does W
Residences have", "golden visa") go to BM25 search over the page text. Photos are mapped to their listing or
project with a label: Wasalt's own room classification (Facade, Kitchen, Bathroom...) or DarGlobal's caption
(the CMS alt text, e.g. "Infinity Outdoor Pool"), so "show me the kitchen" returns kitchen photos, not a guess.

## Data

| Source | What | How | Kept |
|---|---|---|---|
| darglobal.co.uk | every page in `sitemap.xml`: projects, blog, press, insights; unit inventory per project | `scrape/darglobal.py`: headless Playwright, 2 s between pages, reads each page's `__NEXT_DATA__` | text, project facts, captioned images, units for sale (price, size, bedrooms, status) |
| wasalt.sa | a random sample of listings (sale and rent) from the listing sitemap | `scrape/wasalt_console.js`: runs in a normal Chrome tab, ~1 request/s | price, type, city, district, bedrooms, area, description, coordinates, photos with room labels |

- Both sites allow these paths in `robots.txt`. Wasalt's listing pages sit behind a Cloudflare check that refuses
  automated browsers (headless and Playwright-driven Chrome, even after a human click), so the Wasalt collector
  runs inside a normal browser tab, the same way a person browses, at a polite rate. It does not bypass the check.
- Personal data is dropped: owner, agent and broker names, phone numbers, emails, licence numbers. The DarGlobal
  unit records also carry the developer's bank account details; only named property fields are kept.
- Snapshot date and counts are shown in the app header and at `/api/health`.

## Decisions

| Decision | Why | Considered and not used |
|---|---|---|
| SQLite: typed tables + FTS5 in one file | a few thousand rows; exact filters; ships inside the image; no extra service | Postgres/pgvector, Meilisearch, hosted vector DB: an extra service for no gain at this size. DuckDB: analytics strengths unused, weaker FTS |
| Fixed filter tools, not text-to-SQL | exact and safe; free models call tools more reliably than they write SQL | text-to-SQL |
| No embeddings in v1 | the main questions are filters; BM25 + the model's own keyword choice covers text | hybrid BM25 + multilingual embeddings (RRF): next step only if the eval shows misses, mainly Arabic vs English |
| Agent loop with a "sufficient context" check | Google Research's agentic RAG: search again when part of the question is uncovered; done in one loop to save free-tier calls | separate planner / rewriter / checker agents (3-5x the calls) |
| Structured data from the sites' own JSON | the pages already carry typed data (`__NEXT_DATA__`) | LLM extraction at ingest (Google LangExtract): not needed when the source is structured. Google's Open Knowledge Format: a packaging format, not a retrieval method. PageIndex / vectorless tree search: suits long documents; these pages are short |
| Photo labels from the source | Wasalt classifies each photo; DarGlobal has alt text | captioning with a vision model: thousands of photos vs a 50 requests/day free tier |
| Arabic: digits and letter forms normalised at index and query time; "al-" matched both ways | the same word is written several ways | an Arabic stemmer |

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add OPENROUTER_API_KEY
.venv/bin/python -m app.build_kb   # data/raw/*.jsonl -> data/kb.sqlite
.venv/bin/uvicorn app.main:app --env-file .env --port 7860
```

Docker:

```bash
docker build -t property-assistant .
docker run -p 7860:7860 -e OPENROUTER_API_KEY=... property-assistant
```

Refresh the data:

```bash
.venv/bin/pip install -r requirements-scrape.txt && .venv/bin/python -m playwright install chromium
.venv/bin/python scrape/darglobal.py           # resumes if interrupted
# Wasalt: open https://wasalt.sa/en in Chrome, paste scrape/wasalt_console.js into the DevTools console,
# then move the downloaded wasalt_listings.jsonl to data/raw/
.venv/bin/python -m app.build_kb
```

## Evaluation

`eval/` holds a golden set of questions (English and Arabic) with answers checked against direct SQL, and a
runner that checks: the numbers in the reply, that every linked URL exists in the knowledge base, and the reply
language. Results: `eval/results.md`.

## Limits

- Wasalt data is a sample of the portal on the snapshot date, not live; counts and averages say "in our sample".
- Free models: about 50 requests a day per account on OpenRouter's free tier; a question takes 2-4. The app keeps
  a daily budget, caches first questions, and says so when the quota is used up.
- Information only, not financial advice.
