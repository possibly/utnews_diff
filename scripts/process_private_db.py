#!/usr/bin/env python3
"""Append public edit/removal events from a private observation database to JSONL."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_PRIVATE_DB = Path("private.db")
DEFAULT_EVENTS_JSONL = Path("data") / "events.jsonl"
MONITOR_DAYS = 14
EVENT_FIELDS = ("id", "outlet", "url", "date", "event_type", "original_text", "changed_text")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_iso_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _split_paragraph_into_sentences(para: str) -> list[str]:
    """Split a single normalized paragraph into sentences, respecting common abbreviations."""
    # Use a placeholder to protect known abbreviations from being split on.
    # Covers: single-letter initials (U. S., U.K.), titles (Mr., Dr., Sen., Rep., etc.)
    ABBREV = re.compile(
        r'\b(?:'
        r'Mr|Mrs|Ms|Dr|Prof|Sr|Jr|Rev|Gov|Lt|Sgt|Cpl|Pvt|Pfc|Maj|Col|Gen|Adm|Capt|Supt'
        r'|Sen|Rep|Atty|Asst|Assoc'
        r'|Dept|Corp|Inc|Ltd|Co|Bros|vs|etc|approx|est|vol|no|pp'
        r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec'
        r'|St|Ave|Blvd|Rd|Mt|Ft'
        r')\.(?=\s)',
        re.IGNORECASE,
    )
    # Protect single-letter initials like "U. S." or "U.S." (e.g. "U. S. President")
    INITIAL = re.compile(r'\b([A-Z])\.\s*(?=[A-Z]\.|\s*[A-Z][a-z])')
    PLACEHOLDER = '\x00'
    protected = ABBREV.sub(lambda m: m.group(0).replace('.', PLACEHOLDER), para)
    protected = INITIAL.sub(lambda m: m.group(0).replace('.', PLACEHOLDER), protected)

    OPEN_QUOTES = '"\u201c\u2018'
    parts = re.split(r"(?<=[.!?])\s*(?=[A-Z0-9" + re.escape(OPEN_QUOTES) + r"])", protected)
    return [p.strip().replace(PLACEHOLDER, '.') for p in parts if p.strip()]


def sentence_list(text: str) -> list[str]:
    """Return a flat list of sentences from a multi-paragraph body_text.

    Paragraph breaks are treated as hard sentence boundaries so that sentences
    from adjacent paragraphs are never merged and diffs stay contained within
    paragraph boundaries.
    """
    if not text:
        return []
    sentences = []
    for para in text.split('\n\n'):
        para = normalize_text(para)
        if para:
            sentences.extend(_split_paragraph_into_sentences(para))
    return sentences


def require_private_schema(con: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in con.execute("PRAGMA table_info(article_observations)").fetchall()
    }
    required = {
        "outlet",
        "section",
        "slug",
        "observed_at",
        "url",
        "found",
        "body_text",
    }
    missing = sorted(required - columns)
    if missing:
        raise SystemExit(f"private.db is missing article_observations columns: {', '.join(missing)}")


def load_observations(con: sqlite3.Connection) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    con.row_factory = sqlite3.Row
    require_private_schema(con)
    rows = con.execute(
        """
        SELECT outlet, section, slug, observed_at, url, found, body_text
        FROM article_observations
        ORDER BY outlet, section, slug, observed_at
        """
    ).fetchall()
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["outlet"], row["section"], row["slug"])
        groups.setdefault(key, []).append(dict(row))
    return groups


def event_id(event: dict[str, Any]) -> str:
    payload = "\0".join(
        str(event.get(key) or "")
        for key in ("outlet", "url", "date", "event_type", "original_text", "changed_text")
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def finalized_event(event: dict[str, Any]) -> dict[str, Any]:
    event = dict(event)
    event["id"] = event_id(event)
    return {field: event.get(field) for field in EVENT_FIELDS}


def looks_like_headline_list(text: str) -> bool:
    """Return True if text looks like a rotating 'Latest headlines' widget list.

    Such lists consist entirely of short title-case fragments with no
    sentence-ending punctuation — they contain no real article prose.
    """
    if not text:
        return False
    sentences = sentence_list(text)
    if not sentences:
        return False
    headline_count = sum(
        1 for s in sentences
        if len(s) < 150 and not re.search(r'[.!?]["\'\u201d\u2019)»]?\s*$', s)
    )
    return headline_count == len(sentences)


def changed_event(outlet: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    old_sentences = sentence_list(previous.get("body_text") or "")
    new_sentences = sentence_list(current.get("body_text") or "")
    if old_sentences == new_sentences:
        return None
    # If the previous observation had no body text, this is a first-capture event,
    # not a meaningful edit — skip it.
    if not old_sentences:
        return None

    changes = []
    matcher = difflib.SequenceMatcher(a=old_sentences, b=new_sentences, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        orig = " ".join(old_sentences[a0:a1])
        chng = " ".join(new_sentences[b0:b1])
        # Skip changes that are purely rotating widget headlines (no real prose).
        if looks_like_headline_list(orig) or looks_like_headline_list(chng):
            continue
        # Skip pure insertions or pure deletions — a sentence appearing or
        # disappearing without any corresponding replacement is almost always
        # a dynamic sidebar/widget being added or removed, not an editorial edit.
        # Exception: insertions at the start (b0==0, the new sentences start before
        # any existing content) can represent genuine prepended content like
        # "Read the criminal complaint below:".
        if not orig and b0 > 0:
            continue
        if not chng and a0 > 0:
            continue
        changes.append({"original_text": orig, "changed_text": chng})
    if not changes:
        return None

    return finalized_event({
        "outlet": outlet,
        "url": current.get("url") or previous.get("url") or "",
        "date": current["observed_at"],
        "event_type": "text_changed",
        "original_text": "\n\n".join(change["original_text"] for change in changes if change["original_text"]),
        "changed_text": "\n\n".join(change["changed_text"] for change in changes if change["changed_text"]),
    })


REMOVED_TEXT_WITHHELD_NOTE = (
    "Article was previously observed and later missing. "
    "Full last-seen article text is withheld from the public event log."
)


def removed_event(outlet: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    return finalized_event({
        "outlet": outlet,
        "url": previous.get("url") or current.get("url") or "",
        "date": current["observed_at"],
        "event_type": "removed_or_taken_down",
        "original_text": REMOVED_TEXT_WITHHELD_NOTE,
        "changed_text": None,
    })


def build_events(groups: dict[tuple[str, str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for (outlet, _section, _slug), observations in groups.items():
        if len(observations) < 2:
            continue
        first_seen = parse_iso_datetime(observations[0]["observed_at"])
        if first_seen is None:
            continue
        monitor_until = first_seen + dt.timedelta(days=MONITOR_DAYS)
        previous = observations[0]
        for current in observations[1:]:
            observed_at = parse_iso_datetime(current["observed_at"])
            if observed_at is None or observed_at > monitor_until:
                previous = current
                continue
            if previous.get("found") and current.get("found"):
                event = changed_event(outlet, previous, current)
                if event:
                    events.append(event)
            elif previous.get("found") and not current.get("found"):
                events.append(removed_event(outlet, previous, current))
            previous = current
    return sorted(events, key=lambda event: (event["date"], event["id"]), reverse=True)


def parse_event_line(path: Path, lineno: int, line: str) -> dict[str, Any]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}:{lineno}: invalid JSONL event: {exc}") from exc
    if not isinstance(event, dict):
        raise SystemExit(f"{path}:{lineno}: event line is not an object")
    missing = [field for field in EVENT_FIELDS if field not in event]
    if missing:
        raise SystemExit(f"{path}:{lineno}: missing event fields: {', '.join(missing)}")
    return {field: event.get(field) for field in EVENT_FIELDS}


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            events.append(parse_event_line(path, lineno, line))
    return events


def write_events_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        unique[str(event["id"])] = {field: event.get(field) for field in EVENT_FIELDS}
    ordered = sorted(unique.values(), key=lambda event: (event.get("date") or "", event.get("id") or ""), reverse=True)
    content = "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in ordered)
    path.write_text(content, encoding="utf-8")


def append_new_events(path: Path, candidate_events: list[dict[str, Any]]) -> tuple[int, int]:
    existing_events = read_events(path)
    existing_ids = {str(event["id"]) for event in existing_events}
    new_events = [event for event in candidate_events if str(event["id"]) not in existing_ids]
    write_events_jsonl(path, [*existing_events, *new_events])
    return len(new_events), len(existing_events) + len(new_events)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append public JSONL events from a private observation DB.")
    parser.add_argument("private_db", nargs="?", type=Path, default=DEFAULT_PRIVATE_DB)
    parser.add_argument("--events-jsonl", type=Path, default=DEFAULT_EVENTS_JSONL)
    args = parser.parse_args()

    if not args.private_db.exists():
        raise SystemExit(f"private database not found: {args.private_db}")

    private_con = sqlite3.connect(args.private_db)
    try:
        candidate_events = build_events(load_observations(private_con))
    finally:
        private_con.close()
    new_count, total_count = append_new_events(args.events_jsonl, candidate_events)
    print(f"Appended {new_count} new public events to {args.events_jsonl} ({total_count} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
