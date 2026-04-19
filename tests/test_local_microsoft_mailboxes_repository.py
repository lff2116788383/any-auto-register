from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from core.db import LocalMicrosoftMailboxModel, engine
from infrastructure.local_microsoft_mailboxes_repository import LocalMicrosoftMailboxesRepository


def test_allocate_handles_naive_datetime_fields_from_sqlite():
    repo = LocalMicrosoftMailboxesRepository()
    base_utc = datetime.now(timezone.utc)
    naive_last_used = (base_utc - timedelta(hours=2)).replace(tzinfo=None)
    naive_leased_until = (base_utc - timedelta(minutes=5)).replace(tzinfo=None)
    naive_cooldown_until = (base_utc - timedelta(minutes=1)).replace(tzinfo=None)


    with Session(engine) as session:
        row = LocalMicrosoftMailboxModel(
            pool="default",
            email="naive@example.com",
            password="secret",
            status="cooldown",
            last_used_at=naive_last_used,
            leased_until=naive_leased_until,
            cooldown_until=naive_cooldown_until,
        )
        session.add(row)
        session.commit()

    allocated = repo.allocate(
        pool="default",
        platform="chatgpt",
        leased_by_task_id="task_test",
        route_strategy="score",
    )

    assert allocated is not None
    assert allocated.email == "naive@example.com"
    assert allocated.status == "leased"
    assert allocated.leased_by_task_id == "task_test"
    assert allocated.last_used_at is not None
    assert allocated.leased_until is not None
