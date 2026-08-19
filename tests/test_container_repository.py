"""ContainerRepository.apply_provider_result - direct unit coverage for the
event-derived summary fields (last_known_location/vessel/voyage) and event
deduplication, tested against the real (SQLite-in-tests) DB session rather
than through the HTTP layer, since this is pure business logic with no
routing/auth concerns to cover.

Each test below mirrors a specific bug report from a manual QA pass over
real production data (container numbers kept for traceability back to that
report) - see repositories/containers.py's `_most_recent_actual_value` for
the rule these encode: last_known_location/vessel/voyage always come from
the most recent *actual* event, never an estimated/future one and never a
value that's actually a vessel name leaking into the location field.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.security import hash_token
from app.models.api_key import ApiKey
from app.providers.base import NormalizedEvent, NormalizedTrackingResult
from app.repositories.containers import ContainerRepository


def _org_id_for(db_session, api_key: str):
    return db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one().organization_id


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _new_container(db_session, api_key, number: str):
    repo = ContainerRepository()
    org_id = _org_id_for(db_session, api_key)
    container, _created = repo.get_or_create(db_session, organization_id=org_id, container_number=number)
    db_session.commit()
    return repo, container


def test_duplicate_events_within_one_provider_result_are_deduped(db_session, api_key):
    """BLZU1200310: a single provider result can itself contain the exact
    same event twice - the old dedup only checked against already-persisted
    events, not against events already added earlier in the same batch."""
    repo, container = _new_container(db_session, api_key, "BLZU1200310")

    occurred_at = _dt(2026, 8, 1)
    result = NormalizedTrackingResult(
        ok=True,
        events=[
            NormalizedEvent(event_code="gate_in", location="Singapore", occurred_at=occurred_at, actual=True),
            NormalizedEvent(event_code="gate_in", location="Singapore", occurred_at=occurred_at, actual=True),
        ],
    )
    new_events = repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()

    assert len(new_events) == 1
    db_session.refresh(container)
    assert len(container.events) == 1


def test_a_second_scrape_still_does_not_duplicate_against_persisted_events(db_session, api_key):
    """The pre-existing dedup path (against already-committed events) must
    keep working alongside the new within-batch check."""
    repo, container = _new_container(db_session, api_key, "BLZU1200311")
    occurred_at = _dt(2026, 8, 1)

    first = NormalizedTrackingResult(
        ok=True, events=[NormalizedEvent(event_code="gate_in", location="Singapore", occurred_at=occurred_at, actual=True)]
    )
    repo.apply_provider_result(db_session, container, result=first)
    db_session.commit()

    second = NormalizedTrackingResult(
        ok=True, events=[NormalizedEvent(event_code="gate_in", location="Singapore", occurred_at=occurred_at, actual=True)]
    )
    new_events = repo.apply_provider_result(db_session, container, result=second)
    db_session.commit()

    assert new_events == []
    db_session.refresh(container)
    assert len(container.events) == 1


def test_last_known_location_ignores_a_future_estimated_event(db_session, api_key):
    """TMCU1400127: the provider's own top-level guess ("Jeddah") was a
    *future* leg of the route, not the last confirmed location. The most
    recent *actual* event (Al Adabiyah, 13 Aug) must win instead."""
    repo, container = _new_container(db_session, api_key, "TMCU1400127")

    result = NormalizedTrackingResult(
        ok=True,
        status="In Transit",
        location="Jeddah",  # the old top-level-guess bug
        events=[
            NormalizedEvent(event_code="arrived", location="Al Adabiyah", occurred_at=_dt(2026, 8, 4), actual=True),
            NormalizedEvent(event_code="arrived", location="Al Adabiyah", occurred_at=_dt(2026, 8, 13), actual=True),
            NormalizedEvent(event_code="expected", location="Al Adabiyah", occurred_at=_dt(2026, 8, 26), actual=False),
            NormalizedEvent(event_code="expected", location="Jeddah", occurred_at=_dt(2026, 8, 28), actual=False),
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Al Adabiyah"


def test_last_known_location_stays_correct_for_the_case_that_already_worked(db_session, api_key):
    """ITXU1020776: the report calls this one out as *already* correct
    under the old logic ("Perfect. That's logical.") - a regression check
    that the new derivation doesn't turn a working case into a broken one.
    Same shape as TMCU1400127 (actual events, then a future leg), but here
    the most recent actual event's location and the provider's own
    top-level guess happen to agree."""
    repo, container = _new_container(db_session, api_key, "ITXU1020776")

    result = NormalizedTrackingResult(
        ok=True,
        status="In Transit",
        location="Laem Chabang",
        events=[
            NormalizedEvent(event_code="arrived", location="Laem Chabang", occurred_at=_dt(2026, 8, 13), actual=True),
            NormalizedEvent(event_code="arrived", location="Laem Chabang", occurred_at=_dt(2026, 8, 15), actual=True),
            NormalizedEvent(event_code="expected", location="Laem Chabang", occurred_at=_dt(2026, 8, 22), actual=False),
            NormalizedEvent(event_code="expected", location="Tanjung Pelepas", occurred_at=_dt(2026, 8, 26), actual=False),
            NormalizedEvent(event_code="expected", location="Tanjung Pelepas", occurred_at=_dt(2026, 9, 4), actual=False),
            NormalizedEvent(event_code="expected", location="Rotterdam", occurred_at=_dt(2026, 9, 26), actual=False),
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Laem Chabang"


def test_last_known_location_picks_the_latest_actual_event_not_list_order(db_session, api_key):
    """SMCU2010117: events arrived out of chronological list-order in the
    provider payload (Laem Chabang entries after Ho Chi Minh City ones, but
    also interleaved) - selection must go by `occurred_at`, not by which
    event happens to be last/first in the list."""
    repo, container = _new_container(db_session, api_key, "SMCU2010117")

    result = NormalizedTrackingResult(
        ok=True,
        status="DELIVERED",
        location="Ho Chi Minh City",  # the old top-level-guess bug
        events=[
            NormalizedEvent(event_code="a", location="Ho Chi Minh City", occurred_at=_dt(2026, 6, 16), actual=True),
            NormalizedEvent(event_code="b", location="Ho Chi Minh City", occurred_at=_dt(2026, 6, 27), actual=True),
            NormalizedEvent(event_code="c", location="Ho Chi Minh City", occurred_at=_dt(2026, 6, 29), actual=True),
            NormalizedEvent(event_code="d", location="Laem Chabang", occurred_at=_dt(2026, 7, 1), actual=True),
            NormalizedEvent(event_code="e", location="Laem Chabang", occurred_at=_dt(2026, 7, 22), actual=True),
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Laem Chabang"


def test_last_known_location_progresses_past_an_earlier_stop(db_session, api_key):
    """BHCU2075548: reported returning "Colombo" despite later actual
    events at Visakhapatnam - same root cause/fix as SMCU2010117, kept as
    its own test since the report named it as a separate example."""
    repo, container = _new_container(db_session, api_key, "BHCU2075548")

    result = NormalizedTrackingResult(
        ok=True,
        location="Colombo",  # the old top-level-guess bug
        events=[
            NormalizedEvent(event_code="a", location="Colombo", occurred_at=_dt(2026, 6, 1), actual=True),
            NormalizedEvent(event_code="b", location="Visakhapatnam", occurred_at=_dt(2026, 6, 10), actual=True),
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Visakhapatnam"


def test_last_known_location_skips_the_unknown_placeholder(db_session, api_key):
    """PCLU2023078: top-level location was literally the string "unknown"
    despite a real actual event (Busan) existing in the timeline."""
    repo, container = _new_container(db_session, api_key, "PCLU2023078")

    result = NormalizedTrackingResult(
        ok=True,
        location="unknown",
        events=[NormalizedEvent(event_code="vad", location="Busan", occurred_at=_dt(2019, 5, 19), actual=True)],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Busan"


def test_vessel_and_voyage_come_from_the_most_recent_actual_event_with_a_value(db_session, api_key):
    """SKHU1500494 / SMCU2010117: top-level vessel/voyage were null even
    though an event clearly carried them - the old code took `events[-1]`
    unconditionally, so a later event with no vessel/voyage data (e.g. a
    customs hold) blanked out an earlier, real vessel/voyage."""
    repo, container = _new_container(db_session, api_key, "SKHU1500494")

    result = NormalizedTrackingResult(
        ok=True,
        status="In Transit",
        vessel=None,
        voyage=None,
        events=[
            NormalizedEvent(
                event_code="loaded", vessel="HONOR OCEAN", voyage="1072W", occurred_at=_dt(2026, 7, 1), actual=True
            ),
            NormalizedEvent(
                event_code="customs_hold", vessel=None, voyage=None, occurred_at=_dt(2026, 7, 5), actual=True
            ),
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.vessel == "HONOR OCEAN"
    assert container.voyage == "1072W"


def test_vessel_stays_correct_for_the_case_that_already_worked(db_session, api_key):
    """MERU4090782: the report calls this one out as already correct
    ("vessel": "EVER UNITED" - Makes sense) - a regression check that the
    new derivation doesn't change a case that was never broken."""
    repo, container = _new_container(db_session, api_key, "MERU4090782")

    result = NormalizedTrackingResult(
        ok=True,
        status="In Transit",
        vessel="EVER UNITED",
        events=[
            NormalizedEvent(
                event_code="loaded", vessel="EVER UNITED", voyage="045E", occurred_at=_dt(2026, 7, 1), actual=True
            )
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.vessel == "EVER UNITED"


def test_location_excludes_a_value_that_is_actually_a_vessel_name(db_session, api_key):
    """HALU1001131 ("EPIC REEFER"): some upstream events carry a vessel
    name in the `location` field. Since "EPIC REEFER" is independently
    known to be this container's vessel (from an earlier event), it must
    not be selected as last_known_location - fall back to the last real
    place name instead."""
    repo, container = _new_container(db_session, api_key, "HALU1001131")

    result = NormalizedTrackingResult(
        ok=True,
        events=[
            NormalizedEvent(
                event_code="loaded", location="Rotterdam", vessel="EPIC REEFER", occurred_at=_dt(2026, 7, 1), actual=True
            ),
            NormalizedEvent(
                event_code="cer", location="EPIC REEFER", vessel="EPIC REEFER", occurred_at=_dt(2026, 7, 10), actual=True
            ),
        ],
    )
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Rotterdam"
    assert container.vessel == "EPIC REEFER"  # a real vessel name is fine in the vessel field


def test_falls_back_to_the_providers_top_level_location_when_there_are_no_actual_events(db_session, api_key):
    """The two browser-based providers report a location but no structured
    event timeline at all - derivation must still fall back gracefully."""
    repo, container = _new_container(db_session, api_key, "NOEV1234567")

    result = NormalizedTrackingResult(ok=True, status="In Transit", location="Singapore", events=[])
    repo.apply_provider_result(db_session, container, result=result)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Singapore"


def test_never_blanks_a_previously_known_value_when_nothing_new_qualifies(db_session, api_key):
    """A later scrape with only estimated/no events must not erase a
    last_known_location that was already correctly derived earlier."""
    repo, container = _new_container(db_session, api_key, "KEEP1234567")

    first = NormalizedTrackingResult(
        ok=True, events=[NormalizedEvent(event_code="arrived", location="Busan", occurred_at=_dt(2026, 7, 1), actual=True)]
    )
    repo.apply_provider_result(db_session, container, result=first)
    db_session.commit()

    second = NormalizedTrackingResult(
        ok=True,
        location=None,
        events=[NormalizedEvent(event_code="expected", location="Rotterdam", occurred_at=_dt(2026, 8, 1), actual=False)],
    )
    repo.apply_provider_result(db_session, container, result=second)
    db_session.commit()
    db_session.refresh(container)

    assert container.last_known_location == "Busan"
