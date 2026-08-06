"""
Smoke test + timing benchmark for the Track-Trace API.

Tests one container via GET /v1/track/{number}, then 15 containers in
parallel via POST /v1/track, and prints timing from two sources:
  - X-Process-Time response header -> total server-side request time
  - duration_seconds field per item -> time scrape_container() itself took
"""
import time
import requests

BASE_URL = "http://127.0.0.1:8000"
HEADERS = {
    "X-API-Key": "test-api-key-123",
    "Content-Type": "application/json"
}

# 10 real sample containers + 5 synthetic ones to reach 15 unique (uncached) numbers
CONTAINERS_15 = [
    "ZWFU1000133", "BHCU2075548", "ROMU2210313", "MRTU2004432",
    "ZMOU8967501", "MSKU5451595", "CAKU3101190", "HDMU2450550",
    "NSRU8004625", "BWLU2100015",
    "TESTU1000001", "TESTU1000002", "TESTU1000003", "TESTU1000004", "TESTU1000005",
]


def print_stats(label, items, wall_time):
    durations = [i["duration_seconds"] for i in items if i.get("duration_seconds") is not None]
    print(f"\n--- {label} ---")
    print(f"Client wall time : {wall_time:.2f}s")
    if durations:
        print(f"Scrape durations : min={min(durations):.2f}s  max={max(durations):.2f}s  "
              f"avg={sum(durations)/len(durations):.2f}s  sum={sum(durations):.2f}s")
        print(f"Parallel speedup : {sum(durations)/wall_time:.2f}x "
              f"(sum of individual scrape times / actual wall time)")
    for i in items:
        d = f"{i['duration_seconds']:.2f}s" if i.get("duration_seconds") is not None else "cached"
        print(f"  - {i['container_number']:15s} status={i['status']:35s} duration={d}")


def bench_single(container_number):
    start = time.perf_counter()
    try:
        resp = requests.get(f"{BASE_URL}/v1/track/{container_number}", headers=HEADERS)
        wall = time.perf_counter() - start
        resp.raise_for_status()
        print(f"Server X-Process-Time header: {resp.headers.get('X-Process-Time')}s")
        print_stats("SINGLE container", [resp.json()], wall)
    except Exception as e:
        print(f"Error connecting to API (single): {e}")


def bench_batch(container_numbers):
    start = time.perf_counter()
    try:
        resp = requests.post(f"{BASE_URL}/v1/track", headers=HEADERS, json={"container_numbers": container_numbers})
        wall = time.perf_counter() - start
        resp.raise_for_status()
        print(f"Server X-Process-Time header: {resp.headers.get('X-Process-Time')}s")
        print_stats(f"BATCH of {len(container_numbers)} containers (parallel)", resp.json(), wall)
    except Exception as e:
        print(f"Error connecting to API (batch): {e}")


def test_api():
    bench_single(CONTAINERS_15[0])
    bench_batch(CONTAINERS_15)


if __name__ == "__main__":
    test_api()
