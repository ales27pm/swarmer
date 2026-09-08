from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.services.control_plane_instance import (
    ControlPlaneInstanceConflict,
    ControlPlaneInstanceService,
    safe_hostname_label,
)
from app.services.state_service import SCHEMA_VERSION, StateService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_boot_instances_are_random_and_hostname_is_only_safe_metadata(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    first = ControlPlaneInstanceService(
        database,
        version="0.10.0",
        hostname="Ubuntu Host/../../SECRET",
        clock=clock,
    )
    second = ControlPlaneInstanceService(
        database,
        version="0.10.0",
        hostname="Ubuntu Host/../../SECRET",
        clock=clock,
    )

    first_record = await first.start()
    second_record = await second.start()

    assert first_record.instance_id != second_record.instance_id
    assert first_record.instance_id != first_record.hostname_label
    assert first_record.instance_id.startswith("cp_")
    assert first_record.hostname_label == "ubuntu-host-secret"
    assert second_record.hostname_label == first_record.hostname_label
    assert first_record.version == "0.10.0"
    assert set(first_record.public_status()) == {
        "instance_id",
        "hostname_label",
        "version",
        "started_at",
        "heartbeat_at",
        "stopped_at",
    }


def test_hostname_label_is_bounded_and_safe() -> None:
    assert safe_hostname_label("../") == "host"
    label = safe_hostname_label("Ä" + ("Host Name/" * 20))
    assert len(label) <= 48
    assert label.replace("-", "").isalnum()
    assert "/" not in label
    assert ".." not in label


@pytest.mark.asyncio
async def test_instance_lifecycle_is_persisted_and_stopped_identity_cannot_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    service = ControlPlaneInstanceService(
        database,
        version="0.10.0",
        instance_id="cp_test_boot_a",
        hostname="ubuntu-a",
        clock=clock,
    )

    started = await service.start()
    assert started.started_at == clock().isoformat()
    assert started.heartbeat_at == started.started_at
    assert started.stopped_at is None

    clock.advance(5)
    heartbeat = await service.heartbeat()
    assert heartbeat.started_at == started.started_at
    assert heartbeat.heartbeat_at == clock().isoformat()

    clock.advance(5)
    stopped = await service.stop()
    assert stopped.stopped_at == clock().isoformat()
    assert stopped.heartbeat_at == stopped.stopped_at
    assert (await service.stop()).stopped_at == stopped.stopped_at

    with pytest.raises(ControlPlaneInstanceConflict, match="stopped"):
        await service.heartbeat()
    with pytest.raises(ControlPlaneInstanceConflict, match="cannot restart"):
        await service.start()


@pytest.mark.asyncio
async def test_instance_registration_is_idempotent_but_metadata_is_immutable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    await StateService(database).initialize()
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    service = ControlPlaneInstanceService(
        database,
        version="0.10.0",
        instance_id="cp_collision_test",
        hostname="ubuntu-a",
        clock=clock,
    )
    started = await service.start()
    clock.advance(2)
    repeated = await service.start()
    assert repeated.started_at == started.started_at
    assert repeated.heartbeat_at == clock().isoformat()

    conflicting = ControlPlaneInstanceService(
        database,
        version="0.10.1",
        instance_id=service.instance_id,
        hostname="ubuntu-a",
        clock=clock,
    )
    with pytest.raises(ControlPlaneInstanceConflict, match="metadata"):
        await conflicting.start()


@pytest.mark.asyncio
async def test_additive_schema_restart_preserves_instance_history(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    state = StateService(database)
    await state.initialize()
    service = ControlPlaneInstanceService(
        database,
        version="0.10.0",
        instance_id="cp_preserved",
        hostname="ubuntu",
    )
    before = await service.start()

    await state.initialize()

    assert await service.get() == before
    async with aiosqlite.connect(database) as db:
        version_row = await (await db.execute("PRAGMA user_version")).fetchone()
        columns = {
            str(row[1])
            for row in await (
                await db.execute("PRAGMA table_info(control_plane_instances)")
            ).fetchall()
        }
    assert version_row is not None and int(version_row[0]) == SCHEMA_VERSION
    assert columns == {
        "instance_id",
        "hostname_label",
        "version",
        "started_at",
        "heartbeat_at",
        "stopped_at",
    }


def test_instance_clock_must_be_timezone_aware(tmp_path: Path) -> None:
    service = ControlPlaneInstanceService(
        tmp_path / "state.db",
        version="0.10.0",
        clock=lambda: datetime(2026, 1, 1),  # noqa: DTZ001 - deliberate invalid clock
    )
    with pytest.raises(RuntimeError, match="timezone-aware"):
        service._now()
