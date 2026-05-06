# utnews_diff

Public append-only event log and static site for Utah local news edit/removal observations.

This repository intentionally contains only the public, auditable part of the system:

1. **Observation contract** — the SQLite schema a private collector must produce.
2. **Processor** — deterministic code that reads a private observation database and appends new public events to `data/events.jsonl`.
3. **Site builder** — deterministic code that reads `data/events.jsonl` and writes `site/index.html`.
4. **Public artifacts** — the append-only JSONL event log and GitHub Pages dashboard.

The live source-specific scraping code, raw observation database, request cadence, and runtime state live in a separate private collector repository. This avoids publishing a turnkey scraping system or raw article-text collection process while keeping the public event encoding auditable and reproducible from any compatible observation database.

## Pipeline

```text
private collector repo
  ↓ produces a private rolling SQLite observation database
article_observations table
  ↓ scripts/process_private_db.py
data/events.jsonl append-only public event log
  ↓ scripts/build_site.py
site/index.html + site/events.jsonl
```

## Inputs and outputs

| Path | Role | Committed? |
|------|------|------------|
| compatible private observation DB | Private SQLite database with article observations | No |
| `data/events.jsonl` | Canonical append-only public event log | Yes |
| `site/index.html` | Generated dashboard | Yes |
| `site/events.jsonl` | Copy of `data/events.jsonl` for GitHub Pages download | No |

The public repository should never contain `private.db`, `private_data/`, raw scraper logs, source-specific collection credentials, or full private article-observation history.

## Observation database contract

A compatible private observation database contains an `article_observations` table:

```sql
CREATE TABLE article_observations (
    outlet      TEXT NOT NULL,
    section     TEXT NOT NULL,
    slug        TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    url         TEXT NOT NULL,
    found       INTEGER NOT NULL,
    body_text   TEXT
);
```

Required columns:

| Column | Description |
|--------|-------------|
| `outlet` | Stable source key, e.g. `ksl`, `sltrib`, `kuer` |
| `section` | Stable section key, e.g. `news` |
| `slug` | Stable article key within an outlet/section |
| `observed_at` | ISO-8601 timestamp for this observation |
| `url` | Article URL |
| `found` | `1` if the article was available, `0` if missing/removed |
| `body_text` | Article body text for private comparison; nullable for missing observations |

The processor groups rows by `(outlet, section, slug)`, orders them by `observed_at`, compares observations inside the first 14 days after first sighting, and appends public events for detected text changes or removals.

## Public `data/events.jsonl` output

`data/events.jsonl` is newline-delimited JSON. Each line is one public event object:

```json
{"id":"...","outlet":"ksl","url":"https://...","date":"2026-05-05T00:00:00Z","event_type":"text_changed","original_text":"...","changed_text":"..."}
```

Fields:

| Field | Meaning |
|-------|---------|
| `id` | Stable event ID derived from event contents |
| `outlet` | Source key |
| `url` | Article URL |
| `date` | Observation timestamp when the event was detected |
| `event_type` | `text_changed` or `removed_or_taken_down` |
| `original_text` | Changed/removed public excerpt or withholding note |
| `changed_text` | Replacement excerpt for text changes; `null` for removals |

Event types:

| Type | Meaning |
|------|---------|
| `text_changed` | Article text changed between two observations |
| `removed_or_taken_down` | Article was previously available and later missing |

Public text policy:

- `text_changed` events include only the changed sentence-level excerpts needed to show the edit.
- `removed_or_taken_down` events do **not** publish the last-seen full article body; they record the URL, outlet, timestamp, and a public withholding note.
- Raw article observations and full article bodies remain private runtime state.

## Local usage

Append public events from a compatible private observation database:

```bash
python3 scripts/process_private_db.py /path/to/private.db
python3 scripts/build_site.py
```

Or write the event log to an explicit path:

```bash
python3 scripts/process_private_db.py /path/to/private.db --events-jsonl data/events.jsonl
```

Preview the dashboard:

```bash
python3 -m http.server 8000 --directory site
```

## Deployment architecture

A separate private collector repository is responsible for:

1. Maintaining private rolling observation state on a VPS or local machine.
2. Running source-specific collectors.
3. Checking out this public repository.
4. Running `scripts/process_private_db.py` and `scripts/build_site.py` from this repository.
5. Committing only `data/events.jsonl` and `site/index.html` back to the public repository.

The public GitHub Pages workflow only publishes the already-generated `site/` directory.

## License and third-party content

The original code, documentation, schema definitions, and project-authored metadata in this repository are dedicated to the public domain under CC0 1.0 Universal. See `LICENSE`.

This repository does **not** claim ownership of, dedicate, or license third-party news article text. Any article excerpts appearing in public event records are included only as factual evidence of detected changes. Raw article bodies are not part of the public license grant and should not be redistributed from private collector state.
