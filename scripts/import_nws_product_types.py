#!/usr/bin/env python3
"""Import the National Weather Service product type catalogue.

The catalogue lists the text products the agency issues, each with a code and
a name. It is stable knowledge about a warning system rather than a
measurement: the codes change when the agency changes its products, which is
rare and meaningful.

Same shape as the glossary importer, and for the same reason: a declarative
mapping extracts single fields and deliberately cannot iterate, so the loop
lives in a reviewed script and its output is committed like any other content.

Usage::

    python scripts/import_nws_product_types.py
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

CATALOGUE_URL = "https://api.weather.gov/products/types"
USER_AGENT = "aladdin-kb-weather/0.1 (+https://github.com/Jin-Aladdin/aladdin-kb-weather)"
TIMEOUT_SECONDS = 60
MAX_BYTES = 4_000_000

SOURCE_ID = "source:weather.nws.product-types"
NAMESPACE = "weather.product-type"

SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return SLUG_STRIP.sub("-", folded.lower()).strip("-") or "unnamed"


def fetch(url: str = CATALOGUE_URL) -> bytes:
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/ld+json")
    request.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise SystemExit(f"catalogue exceeded {MAX_BYTES} bytes")
    return payload


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
        newline="\n",
    )


def build(payload: bytes, retrieved_at: str):
    document = json.loads(payload.decode("utf-8"))
    entries = document.get("@graph") or []
    content_hash = "sha256:" + hashlib.sha256(payload).hexdigest()

    claims, evidence = [], []
    seen: dict[str, int] = {}
    skipped = 0

    for index, entry in enumerate(entries):
        code = (entry.get("productCode") or "").strip()
        name = (entry.get("productName") or "").strip()
        if not code or not name:
            skipped += 1
            continue

        slug = slugify(code)
        occurrence = seen.get(slug, 0) + 1
        seen[slug] = occurrence
        suffix = f".{occurrence:03d}"

        claim_id = f"claim:{NAMESPACE}.{slug}{suffix}"
        evidence_id = f"evidence:{NAMESPACE}.{slug}{suffix}"

        claims.append(
            {
                "id": claim_id,
                "kind": "factual",
                "statement": (
                    f"The U.S. National Weather Service issues a text product with the "
                    f"code {code}, named {name}."
                ),
                "language": "en",
                "status": "source-backed",
                "confidence": 0.95,
                "source_ids": [SOURCE_ID],
                "evidence_ids": [evidence_id],
                "entity_ids": ["entity:weather.nws"],
                "concept_ids": ["concept:weather.warning-system"],
                "created_at": retrieved_at,
                "last_verified_at": retrieved_at,
            }
        )
        evidence.append(
            {
                "id": evidence_id,
                "source_id": SOURCE_ID,
                "claim_ids": [claim_id],
                "locator": {"type": "json-pointer", "pointer": f"/@graph/{index}"},
                "excerpt": f"{code} {name}"[:3900],
                "excerpt_language": "en",
                "captured_at": retrieved_at,
                "verification": {
                    "status": "content-matched",
                    "method": "structured-lookup",
                    "verified_at": retrieved_at,
                },
                "status": "active",
            }
        )

    source = {
        "id": SOURCE_ID,
        "type": "official-api",
        "title": "National Weather Service product type catalogue",
        "publisher": "National Weather Service, NOAA",
        "url": CATALOGUE_URL,
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
            "The catalogue of text products the agency issues. Work of the U.S. "
            "federal government, not subject to domestic copyright, recorded as a "
            "LicenseRef because no SPDX identifier describes that status."
        ),
    }

    if skipped:
        print(f"skipped {skipped} entries without a code or name", file=sys.stderr)
    return claims, evidence, source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--now", default="")
    args = parser.parse_args(argv)

    payload = args.input.read_bytes() if args.input else fetch()
    retrieved_at = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    claims, evidence, source = build(payload, retrieved_at)

    def foreign(records, prefix):
        return [r for r in records if not r["id"].startswith(prefix)]

    cp, ep, sp = (PACK_ROOT / n / f"{n}.jsonl" for n in ("claims", "evidence", "sources"))
    write_jsonl(cp, foreign(read_jsonl(cp), f"claim:{NAMESPACE}.") + claims)
    write_jsonl(ep, foreign(read_jsonl(ep), f"evidence:{NAMESPACE}.") + evidence)
    sources = [r for r in read_jsonl(sp) if r["id"] != SOURCE_ID] + [source]
    write_jsonl(sp, sources)

    print(f"imported {len(claims)} product types")
    print(f"content hash: {source['content_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
