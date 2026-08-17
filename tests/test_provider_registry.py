"""Unit tests for the provider registry - the adapter-pattern seam over
the carrier scrapers (the "hard, expensive part" of the original brief).
No network, no DB: exercises the pure parsing/fallback logic directly
against realistic raw response shapes.
"""

from __future__ import annotations

import pytest

from app.providers.base import NormalizedTrackingResult
from app.providers.registry import ProviderRegistry, RomeuHttpProvider, SearatesHttpProvider
from app.providers.searates_http import SeaRatesTracker

# --- SeaRatesTracker._parse: vessel/location reference-ID resolution ------------
#
# Regression test for a real bug: SeaRates' raw payload can reference a
# vessel/location by numeric ID into a separate lookup table instead of
# embedding the name directly on the event, and the un-fixed `_parse()` used
# to pass that raw ID straight through - so a customer would see "214" where
# a vessel name belongs. No real captured payload of this shape exists in
# the repo (SEARATES_RAW_SUCCESS above already uses pre-resolved names, so
# it doesn't exercise this path at all) - this fixture is synthetic, built
# from the fix's own defensive key-name assumptions.

SEARATES_RAW_WITH_ID_REFERENCES = {
    "status": "success",
    "message": "",
    "data": {
        "metadata": {"number": "MSKU1234567", "status": "In Transit"},
        "locations": [],
        "route": {},
        "references": {
            "vessels": [{"id": 501, "name": "MSC AURORA"}],
            "locations": [{"id": 900, "name": "Shanghai"}, {"id": 901, "name": "Rotterdam"}],
        },
        "containers": [
            {
                "number": "MSKU1234567",
                "status": "Departed",
                "events": [
                    {
                        "order_id": 1,
                        "status": "Gate In",
                        "date": "2026-01-01 10:00:00",
                        "actual": True,
                        "location": 900,  # numeric ID, not a name
                        "vessel": 501,  # numeric ID, not a name
                        "voyage": "123W",
                    },
                    {
                        "order_id": 2,
                        "status": "Departed",
                        "date": "2026-01-05",
                        "actual": True,
                        "location": 999,  # no matching reference entry
                        "vessel": None,
                        "voyage": "123W",
                    },
                ],
            }
        ],
    },
}


def test_parse_resolves_vessel_and_location_ids_via_references_table():
    parsed = SeaRatesTracker._parse(SEARATES_RAW_WITH_ID_REFERENCES)
    events = parsed["containers"][0]["events"]

    assert events[0]["location"] == "Shanghai"
    assert events[0]["vessel"] == "MSC AURORA"

    # No matching reference entry - falls back to the raw value unresolved,
    # never silently dropped.
    assert events[1]["location"] == 999
    assert events[1]["vessel"] is None


def test_parse_falls_back_to_top_level_vessels_locations_when_no_references_key():
    raw = {
        "status": "success",
        "data": {
            "metadata": {},
            "vessels": [{"id": "V1", "name": "EVER GIVEN"}],
            "locations": [{"id": "L1", "name": "Singapore"}],
            "containers": [
                {
                    "number": "X",
                    "status": "In Transit",
                    "events": [
                        {"order_id": 1, "status": "Departed", "date": None, "actual": True, "location": "L1", "vessel": "V1", "voyage": None}
                    ],
                }
            ],
        },
    }
    parsed = SeaRatesTracker._parse(raw)
    event = parsed["containers"][0]["events"][0]
    assert event["location"] == "Singapore"
    assert event["vessel"] == "EVER GIVEN"


def test_parse_still_passes_through_already_resolved_names_unchanged():
    """No references table at all (today's fixture shape) - names pass
    through untouched, exactly as before this fix."""
    raw = {
        "status": "success",
        "data": {
            "metadata": {},
            "containers": [
                {
                    "number": "X",
                    "status": "In Transit",
                    "events": [
                        {"order_id": 1, "status": "Departed", "date": None, "actual": True, "location": "Shanghai", "vessel": "MSC AURORA", "voyage": None}
                    ],
                }
            ],
        },
    }
    parsed = SeaRatesTracker._parse(raw)
    event = parsed["containers"][0]["events"][0]
    assert event["location"] == "Shanghai"
    assert event["vessel"] == "MSC AURORA"


# --- SearatesHttpProvider._adapt -----------------------------------------------

SEARATES_RAW_SUCCESS = {
    "status": "success",
    "message": "",
    "data": {},  # not read directly - _adapt reads from the already-parsed shape
    "shipment_status": "In Transit",
    "locations": [{"name": "Singapore"}, {"name": "Rotterdam"}],
    "containers": [
        {
            "number": "MSKU1234567",
            "status": "Departed",
            "events": [
                {
                    "status": "Gate In",
                    "date": "2026-01-01 10:00:00",
                    "actual": True,
                    "location": "Shanghai",
                    "vessel": "MSC AURORA",
                    "voyage": "123W",
                },
                {
                    "status": "Departed",
                    "date": "2026-01-05",
                    "actual": True,
                    "location": "Shanghai",
                    "vessel": "MSC AURORA",
                    "voyage": "123W",
                },
            ],
        }
    ],
}


def test_searates_http_adapt_parses_status_location_and_events():
    result = SearatesHttpProvider._adapt(SEARATES_RAW_SUCCESS)
    assert result.ok is True
    assert result.status == "In Transit"
    assert result.location == "Rotterdam"  # last entry in `locations`
    assert result.vessel == "MSC AURORA"
    assert len(result.events) == 2
    assert result.events[0].event_code == "gate_in"
    assert result.events[0].occurred_at is not None
    assert result.events[1].event_code == "departed"


def test_searates_http_adapt_handles_no_containers_as_miss():
    result = SearatesHttpProvider._adapt({"status": "error", "message": "API_KEY_LIMIT_REACHED", "containers": []})
    assert result.ok is False
    assert "API_KEY_LIMIT_REACHED" in result.error


def test_searates_http_adapt_tolerates_missing_optional_fields():
    result = SearatesHttpProvider._adapt({"containers": [{"number": "X", "status": None, "events": []}]})
    assert result.ok is True
    assert result.events == []
    assert result.location is None


# --- RomeuHttpProvider._adapt ---------------------------------------------------

ROMEU_RAW_SUCCESS = {
    "status": "success",
    "number": "ROMU2210313",
    "containers": [
        {
            "container_id": 1,
            "number": "ROMU2210313",
            "status": "Discharged",
            "last_movement": "Discharged at destination",
            "movements": [
                {"port": "Valencia", "description": "Discharged", "status": "Discharged", "date": "2026-02-01", "code": "DIS"},
                {"port": "Origin Port", "description": "Loaded", "status": "Loaded", "date": "2026-01-20", "code": "LOA"},
            ],
        }
    ],
    "messages": [],
}


def test_romeu_adapt_parses_movements_as_actual_events():
    result = RomeuHttpProvider._adapt(ROMEU_RAW_SUCCESS)
    assert result.ok is True
    assert result.status == "Discharged"
    assert len(result.events) == 2
    assert all(event.actual is True for event in result.events)
    assert result.events[0].event_code == "dis"
    assert result.location == "Valencia"


def test_romeu_adapt_handles_not_found():
    result = RomeuHttpProvider._adapt({"status": "error", "containers": [], "messages": ["Container not found"]})
    assert result.ok is False
    assert "not found" in result.error.lower()


def test_romeu_provider_only_supports_romu_prefix():
    provider = RomeuHttpProvider()
    assert provider.supports("ROMU2210313") is True
    assert provider.supports("romu2210313") is True  # case-insensitive
    assert provider.supports("MSKU1234567") is False


# --- ProviderRegistry fallback/ordering behavior --------------------------------


class _FakeProvider:
    def __init__(self, name: str, *, supports_fn=None, result: NormalizedTrackingResult | None = None, raises: Exception | None = None):
        self.name = name
        self._supports_fn = supports_fn or (lambda number: True)
        self._result = result
        self._raises = raises
        self.calls: list[str] = []

    def supports(self, container_number: str) -> bool:
        return self._supports_fn(container_number)

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        self.calls.append(container_number)
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.mark.asyncio
async def test_registry_returns_first_successful_provider():
    miss = _FakeProvider("miss", result=NormalizedTrackingResult(ok=False, error="not found"))
    hit = _FakeProvider("hit", result=NormalizedTrackingResult(ok=True, status="In Transit"))
    never_called = _FakeProvider("never", result=NormalizedTrackingResult(ok=True, status="should not be reached"))

    registry = ProviderRegistry([miss, hit, never_called])
    result = await registry.track("MSKU1234567")

    assert result.ok is True
    assert result.status == "In Transit"
    assert result.provider_name == "hit"
    assert never_called.calls == []


@pytest.mark.asyncio
async def test_registry_skips_providers_that_do_not_support_the_number():
    romeu_only = _FakeProvider("romeu", supports_fn=lambda n: n.startswith("ROMU"), result=NormalizedTrackingResult(ok=True))
    catch_all = _FakeProvider("catch_all", result=NormalizedTrackingResult(ok=True, status="found"))

    registry = ProviderRegistry([romeu_only, catch_all])
    result = await registry.track("MSKU1234567")

    assert romeu_only.calls == []  # never invoked - supports() returned False
    assert catch_all.calls == ["MSKU1234567"]
    assert result.provider_name == "catch_all"


@pytest.mark.asyncio
async def test_registry_returns_ok_false_when_every_provider_misses():
    """The final miss message is customer-facing (surfaces as
    `tracking_message`) - it must stay neutral and must never name which
    internal providers were tried."""
    a = _FakeProvider("a", result=NormalizedTrackingResult(ok=False, error="a missed"))
    b = _FakeProvider("b", result=NormalizedTrackingResult(ok=False, error="b missed"))

    result = await ProviderRegistry([a, b]).track("UNKNOWN0000001")

    assert result.ok is False
    assert "a" not in result.error.split() and "b" not in result.error.split()
    assert result.error == (
        "Container data is not yet available. Try again later or verify the container number is correct."
    )


@pytest.mark.asyncio
async def test_registry_isolates_a_provider_that_raises():
    broken = _FakeProvider("broken", raises=RuntimeError("boom"))
    healthy = _FakeProvider("healthy", result=NormalizedTrackingResult(ok=True, status="found"))

    result = await ProviderRegistry([broken, healthy]).track("MSKU1234567")

    assert result.ok is True
    assert result.provider_name == "healthy"
