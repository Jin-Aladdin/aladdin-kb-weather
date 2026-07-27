#!/usr/bin/env python3
"""Import the National Weather Service glossary into canonical records.

The glossary is a stable, public-domain body of meteorological definitions:
two consecutive retrievals return byte-identical content. That makes it
suitable as knowledge rather than as a measurement.

This is a one-time import, not an automated source. The declarative mapping
in an update policy extracts single fields; it deliberately cannot iterate
over three thousand entries, because a configuration file that can loop is a
program. So the loop lives here, in a script that a maintainer runs and
reviews, and the result is committed like any other content.

Rerunning it against an unchanged glossary produces identical files, so the
import is verifiable: if the diff is empty, nothing upstream moved.

Usage::

    python scripts/import_nws_glossary.py
    python scripts/import_nws_glossary.py --input glossary.json   # offline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent

GLOSSARY_URL = "https://api.weather.gov/glossary"
USER_AGENT = "aladdin-kb-weather/0.1 (+https://github.com/Jin-Aladdin/aladdin-kb-weather)"
TIMEOUT_SECONDS = 60
MAX_BYTES = 8_000_000

SOURCE_ID = "source:weather.nws.glossary"
NAMESPACE = "weather.glossary"

#: The identifier grammar: lowercase alphanumeric groups joined by . _ or -
SLUG_STRIP = re.compile(r"[^a-z0-9]+")

#: Definitions shorter than this carry no information worth asserting.
MIN_DEFINITION_LENGTH = 20
MAX_DEFINITION_LENGTH = 9000


def slugify(term: str) -> str:
    """Turn a glossary term into an identifier segment.

    Accents are folded rather than dropped, so "Föhn" and "Fohn" do not
    collide into different identifiers for the same concept.
    """
    folded = unicodedata.normalize("NFKD", term)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = SLUG_STRIP.sub("-", ascii_only.lower()).strip("-")
    return slug or "unnamed"


def strip_markup(text: str) -> str:
    """Remove the HTML fragments some definitions carry."""
    without_tags = re.sub(r"<[^>]+>", " ", text)
    unescaped = (
        without_tags.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", unescaped).strip()


def fetch(url: str = GLOSSARY_URL) -> bytes:
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/ld+json")
    request.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise SystemExit(f"glossary exceeded {MAX_BYTES} bytes")
    return payload


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
        newline="\n",
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def build(payload: bytes, retrieved_at: str) -> tuple[list[dict], list[dict], dict]:
    """Turn the glossary payload into claims, evidence and a source record."""
    document = json.loads(payload.decode("utf-8"))
    entries = document.get("glossary") or []
    content_hash = "sha256:" + hashlib.sha256(payload).hexdigest()

    claims: list[dict] = []
    evidence: list[dict] = []
    seen: dict[str, int] = {}
    skipped = 0

    for index, entry in enumerate(entries):
        term = strip_markup(entry.get("term") or "")
        definition = strip_markup(entry.get("definition") or "")

        if not term or len(definition) < MIN_DEFINITION_LENGTH:
            skipped += 1
            continue
        if len(definition) > MAX_DEFINITION_LENGTH:
            definition = definition[: MAX_DEFINITION_LENGTH - 1].rstrip() + "…"

        slug = slugify(term)
        # A term can appear more than once with different definitions. Number
        # them rather than letting one silently overwrite the other.
        occurrence = seen.get(slug, 0) + 1
        seen[slug] = occurrence
        suffix = f".{occurrence:03d}"

        claim_id = f"claim:{NAMESPACE}.{slug}{suffix}"
        evidence_id = f"evidence:{NAMESPACE}.{slug}{suffix}"

        claims.append(
            {
                "id": claim_id,
                "kind": "definition",
                "statement": f"In meteorology as defined by the U.S. National Weather Service, "
                f"{term} is: {definition}",
                "language": "en",
                "status": "source-backed",
                # Faithful transcription of a primary definition. Not raised
                # further: the pack asserts what the source says, not that the
                # definition is the only correct one.
                "confidence": 0.95,
                "source_ids": [SOURCE_ID],
                "evidence_ids": [evidence_id],
                "created_at": retrieved_at,
                "last_verified_at": retrieved_at,
            }
        )

        evidence.append(
            {
                "id": evidence_id,
                "source_id": SOURCE_ID,
                "claim_ids": [claim_id],
                # The exact position in the retrieved document, so a reader can
                # check the definition without re-deriving anything.
                "locator": {"type": "json-pointer", "pointer": f"/glossary/{index}"},
                "excerpt": definition[:3900],
                "excerpt_language": "en",
                "captured_at": retrieved_at,
                "verification": {
                    "status": "content-matched",
                    "method": "structured-lookup",
                    "verified_at": retrieved_at,
                },
                "rights": {
                    "excerpt_allowed": True,
                    "redistribution_allowed": True,
                    "contains_personal_data": False,
                    "contains_sensitive_data": False,
                    "notes": "Work of the U.S. federal government, not subject to domestic copyright.",
                },
                "status": "active",
            }
        )

    source = {
        "id": SOURCE_ID,
        "type": "official-glossary",
        "title": "National Weather Service Glossary",
        "publisher": "National Weather Service, NOAA",
        "url": GLOSSARY_URL,
        "canonical_url": "https://www.weather.gov/forecast-terms",
        "retrieved_at": retrieved_at,
        "language": "en",
        "license": "LicenseRef-us-government-work",
        "license_url": "https://www.weather.gov/disclaimer",
        "content_hash": content_hash,
        "media_type": "application/ld+json",
        "authority": "primary",
        "status": "active",
        "access_method": "api",
        "notes": (
            "Definitions published by a U.S. federal agency. Works of the U.S. "
            "government are not subject to copyright within the United States, "
            "which is recorded as a LicenseRef rather than an SPDX identifier "
            "because no listed licence describes that status. Imported once by "
            "scripts/import_nws_glossary.py; the glossary returns byte-identical "
            "content across retrievals."
        ),
    }

    if skipped:
        print(f"skipped {skipped} entries with no term or too short a definition", file=sys.stderr)

    return claims, evidence, source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=None, help="use a saved payload instead of fetching")
    parser.add_argument("--now", default="", help="timestamp to record, for reproducible runs")
    args = parser.parse_args(argv)

    payload = args.input.read_bytes() if args.input else fetch()
    retrieved_at = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    claims, evidence, source = build(payload, retrieved_at)

    claims_path = PACK_ROOT / "claims" / "claims.jsonl"
    evidence_path = PACK_ROOT / "evidence" / "evidence.jsonl"
    sources_path = PACK_ROOT / "sources" / "sources.jsonl"

    # Keep anything that did not come from this import: hand-written knowledge
    # lives alongside it and must not be replaced by a refresh.
    def foreign(records: list[dict], prefix: str) -> list[dict]:
        return [r for r in records if not r["id"].startswith(prefix)]

    write_jsonl(claims_path, foreign(read_jsonl(claims_path), f"claim:{NAMESPACE}.") + claims)
    write_jsonl(evidence_path, foreign(read_jsonl(evidence_path), f"evidence:{NAMESPACE}.") + evidence)

    sources = [r for r in read_jsonl(sources_path) if r["id"] != SOURCE_ID]
    sources.append(source)
    write_jsonl(sources_path, sources)

    print(f"imported {len(claims)} definitions")
    print(f"content hash: {source['content_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
