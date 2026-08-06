"""Slice tests/fixtures/demo_containers.json into a /v1/track-searates/bulk
request body, for ramping load-test volume without hand-editing curl payloads.

Run from the repo root:
    uv run scripts/build_bulk_payload.py --count 10 --batch-size 10 > /tmp/body.json
    uv run scripts/build_bulk_payload.py --count 500 --batch-size 50 | curl -s -X POST ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo_containers.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10, help="how many unique containers to include")
    ap.add_argument("--batch-size", type=int, default=10, help="concurrent workers (bulk_size in request body)")
    ap.add_argument("--sealine", default="AUTO")
    ap.add_argument("--offset", type=int, default=0, help="start offset into the fixture list, to avoid reusing already-cached numbers across runs")
    args = ap.parse_args()

    all_numbers = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if args.offset + args.count > len(all_numbers):
        print(f"offset+count exceeds fixture size ({len(all_numbers)})", file=sys.stderr)
        raise SystemExit(1)

    numbers = all_numbers[args.offset: args.offset + args.count]
    body = {
        "container_numbers": numbers,
        "batch_size": args.batch_size,
        "sealine": args.sealine,
    }
    print(json.dumps(body))


if __name__ == "__main__":
    main()
