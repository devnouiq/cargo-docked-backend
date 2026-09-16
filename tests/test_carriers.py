from __future__ import annotations

from app.core.carrier_coverage import KNOWN_UNSUPPORTED_PREFIXES, SUPPORTED_CARRIER_PREFIXES


def test_carrier_coverage_requires_api_key(client):
    resp = client.get("/v1/carriers")
    assert resp.status_code == 401


def test_carrier_coverage_returns_the_curated_tables(client, api_key):
    resp = client.get("/v1/carriers", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()

    assert {row["prefix"] for row in body["supported"]} == set(SUPPORTED_CARRIER_PREFIXES)
    assert set(body["known_unsupported"]) == set(KNOWN_UNSUPPORTED_PREFIXES)
    assert body["note"]
