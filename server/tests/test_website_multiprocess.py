"""Independent shared-root admission, process ownership and publication regressions."""

from __future__ import annotations

import asyncio
import fcntl
import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.services.website_branding import InfographicArtistClient
from app.services.website_builder import WebsiteBuild
from app.services.website_dossier import FetchResponse
from app.services.website_publisher import StaticDirectoryPublisher
from app.services.website_workflow import WebsiteWorkflow
from app.services.website_workflow_contracts import (
    WebsiteCommand,
    WebsiteCreate,
    WebsitePublish,
    WebsiteReview,
)
from app.services.website_workflow_store import WebsiteConflict


class FixtureSource:
    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
        body = b"<html><title>Atelier</title><h1>Services</h1><p>Consultation locale.</p></html>"
        return FetchResponse(url, 200, {"content-type": "text/html"}, body)


def workflow(root: Path, *, publisher: Any = None, cls: Any = WebsiteWorkflow) -> WebsiteWorkflow:
    public = root.parent / "published"
    public.mkdir(exist_ok=True)
    return cls(
        root,
        brand_client=InfographicArtistClient(None),
        publisher=publisher or StaticDirectoryPublisher(public, "https://published.example/"),
        fetcher=FixtureSource(),
    )


def create(service: WebsiteWorkflow, suffix: str) -> dict[str, Any]:
    return service.create(
        "owner-a",
        WebsiteCreate(
            request_id=f"create-{suffix}",
            source_url="https://fixture.example/",
            objective="Présenter les services clairement.",
        ),
    )


def capture_request(data: dict[str, Any], suffix: str = "default") -> WebsiteCommand:
    return WebsiteCommand(
        request_id=f"capture-{suffix}", expected_version=data["version"], action="capture"
    )


async def until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def drain(*services: WebsiteWorkflow) -> None:
    tasks = [task for service in services for task in service.tasks]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 10)


async def test_live_capture_survives_peer_initialize_and_reconcile(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Held(WebsiteWorkflow):
        async def _capture(self, data: dict[str, Any]) -> None:
            entered.set()
            await release.wait()
            data["status"] = "captured"

    first = workflow(tmp_path / "private", cls=Held)
    second = workflow(first.root)
    first.initialize()
    try:
        data = create(first, "overlap")
        accepted = first.command("owner-a", data["id"], capture_request(data))
        await asyncio.wait_for(entered.wait(), 5)
        second.initialize()  # Coexistence is required, not an accepted startup failure.
        assert second.reconcile() == 0
        assert second.store.get(data["id"], "owner-a") == first.store.get(data["id"], "owner-a")
        observed = second.store.get(data["id"], "owner-a")
        assert (observed["status"], observed["version"]) == ("capturing", accepted["version"])
        with pytest.raises(WebsiteConflict):
            second.command("owner-a", data["id"], capture_request(observed, "other"))
    finally:
        release.set()
        await drain(first)
        await second.close()
        await first.close()


async def test_exact_command_retry_on_peer_does_not_repeat_work(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class Held(WebsiteWorkflow):
        async def _capture(self, data: dict[str, Any]) -> None:
            calls.append(data["id"])
            entered.set()
            await release.wait()
            data["status"] = "captured"

    first = workflow(tmp_path / "private", cls=Held)
    second = workflow(first.root, cls=Held)
    first.initialize()
    second.initialize()
    try:
        data = create(first, "idempotency")
        request = capture_request(data)
        accepted = first.command("owner-a", data["id"], request)
        await asyncio.wait_for(entered.wait(), 5)
        repeated = second.command("owner-a", data["id"], request)
        assert repeated["id"] == accepted["id"]
        assert repeated["version"] == accepted["version"]
        assert calls == [data["id"]]
        changed = request.model_copy(update={"direction_id": "studio"})
        with pytest.raises(WebsiteConflict):
            second.command("owner-a", data["id"], changed)
        with pytest.raises(KeyError):
            second.command("other-owner", data["id"], request)
        release.set()
        await drain(first, second)
        second.command("owner-a", data["id"], request)
        await drain(first, second)
        assert calls == [data["id"]], "completed acceptance must not be executed again"
    finally:
        release.set()
        await drain(first, second)
        await second.close()
        await first.close()


# Separate interpreters are essential: asyncio locks alone would pass same-process
# interleavings while providing no protection when Uvicorn uses multiple workers.
CHILD = r"""
import asyncio, json, sys
from pathlib import Path
from app.services.website_branding import InfographicArtistClient
from app.services.website_publisher import StaticDirectoryPublisher
from app.services.website_workflow import WebsiteWorkflow
from app.services.website_workflow_contracts import WebsiteCreate, WebsiteCommand

async def main():
    root, signals, prefix, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    class Held(WebsiteWorkflow):
        async def _capture(self, data):
            directory = root / data['id']
            directory.mkdir(exist_ok=True)
            (directory / 'preserved.txt').write_text('already saved', encoding='utf-8')
            (signals / ('started-' + data['id'])).touch()
            while not (signals / 'release').exists():
                await asyncio.sleep(0.01)
            data['status'] = 'captured'
    service = Held(root, brand_client=InfographicArtistClient(None),
                   publisher=StaticDirectoryPublisher(None, None))
    service.initialize()
    accepted = []
    for index in range(count):
        data = service.create('owner-a', WebsiteCreate(request_id=f'create-{prefix}-{index}',
                 source_url='https://fixture.example/', objective='Fixture de concurrence.'))
        request = WebsiteCommand(request_id=f'capture-{prefix}-{index}',
                                 expected_version=data['version'], action='capture')
        accepted.append(service.command('owner-a', data['id'], request))
    print(json.dumps(accepted), flush=True)
    await asyncio.gather(*service.tasks)
    await service.close()
asyncio.run(main())
"""


async def child(
    root: Path, signals: Path, prefix: str, count: int
) -> tuple[asyncio.subprocess.Process, list[dict[str, Any]]]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CHILD,
        str(root),
        str(signals),
        prefix,
        str(count),
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        if not line:
            _, error = await process.communicate()
            pytest.fail(f"fixture child startup failed: {error.decode()}")
        return process, json.loads(line)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


async def stop_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    await process.wait()


async def test_processes_share_two_execution_slots_and_eight_admissions(tmp_path: Path) -> None:
    root, signals = tmp_path / "private", tmp_path / "signals"
    signals.mkdir()
    processes: list[asyncio.subprocess.Process] = []
    peer = workflow(root)
    try:
        first, first_data = await child(root, signals, "first", 4)
        processes.append(first)
        await until(lambda: len(list(signals.glob("started-*"))) == 2)
        second, second_data = await child(root, signals, "second", 4)
        processes.append(second)
        peer.initialize()
        assert peer.reconcile() == 0
        ninth = create(peer, "ninth-job")
        with pytest.raises(WebsiteConflict):
            peer.command("owner-a", ninth["id"], capture_request(ninth))
        assert peer.store.get(ninth["id"], "owner-a")["version"] == 1
        await asyncio.sleep(0.2)
        assert len(list(signals.glob("started-*"))) == 2
        for data in first_data + second_data:
            current = peer.store.get(data["id"], "owner-a")
            assert current["status"] == "capturing"
            assert current["version"] == data["version"]
        (signals / "release").touch()
        for process in processes:
            _, stderr = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, stderr.decode()
        assert len(list(signals.glob("started-*"))) == 8
        assert all(
            peer.store.get(data["id"], "owner-a")["status"] == "captured"
            for data in first_data + second_data
        )
    finally:
        (signals / "release").touch()
        for process in processes:
            await stop_child(process)
        await peer.close()


async def test_live_process_is_excluded_until_abrupt_owner_loss(tmp_path: Path) -> None:
    root, signals = tmp_path / "private", tmp_path / "signals"
    signals.mkdir()
    process, records = await child(root, signals, "crash", 1)
    data = records[0]
    peer = workflow(root)
    try:
        await until(lambda: (signals / f"started-{data['id']}").exists())
        peer.initialize()
        assert peer.reconcile() == 0
        assert peer.store.get(data["id"], "owner-a")["status"] == "capturing"
        with pytest.raises(WebsiteConflict):
            peer.command("owner-a", data["id"], capture_request(data, "competing"))
        await stop_child(process)
        assert peer.reconcile() == 1
        observed = peer.store.get(data["id"], "owner-a")
        assert observed["status"] == "interrupted"
        assert observed["version"] > data["version"]
        assert observed["approval"] is None
        assert peer.reconcile() == 0
        assert (root / data["id"] / "preserved.txt").read_text() == "already saved"
        assert not peer.tasks and not peer.publications
        assert list((tmp_path / "published").iterdir()) == []
        assert len(list(signals.glob("started-*"))) == 1
    finally:
        await stop_child(process)
        await peer.close()


async def test_close_holds_project_until_underlying_thread_finishes(tmp_path: Path) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class ThreadedSource(FixtureSource):
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            entered.set()
            assert release.wait(5)
            (tmp_path / "private" / "thread-finished.txt").write_text("saved")
            finished.set()
            return super().get(url, max_bytes=max_bytes, deadline=deadline)

    first = workflow(tmp_path / "private")
    first.fetcher = ThreadedSource()
    second = workflow(first.root)
    first.initialize()
    second.initialize()
    closing: asyncio.Task[None] | None = None
    try:
        data = create(first, "thread-drain")
        accepted = first.command("owner-a", data["id"], capture_request(data))
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(first.close())
        await asyncio.sleep(0.05)
        assert not closing.done(), "coroutine cancellation released live thread ownership"
        assert not finished.is_set()
        assert second.reconcile() == 0
        assert second.store.get(data["id"], "owner-a")["version"] == accepted["version"]
        release.set()
        await asyncio.wait_for(closing, 5)
        assert finished.is_set()
        assert (first.root / "thread-finished.txt").read_text() == "saved"
        assert second.store.get(data["id"], "owner-a")["status"] in {"captured", "interrupted"}
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, 5)
        await first.close()
        await second.close()


async def built(service: WebsiteWorkflow) -> dict[str, Any]:
    data = create(service, "publication")
    service.command("owner-a", data["id"], capture_request(data))
    await drain(service)
    data = service.store.get(data["id"], "owner-a")
    assert data["status"] == "captured", data
    service.command(
        "owner-a",
        data["id"],
        WebsiteCommand(
            request_id="build-publication",
            expected_version=data["version"],
            action="build",
            palette_id="paper-ink",
        ),
    )
    await drain(service)
    data = service.store.get(data["id"], "owner-a")
    assert data["status"] == "preview_ready", data
    return data


async def test_peer_cannot_replace_approval_or_mutate_live_publication(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    public = tmp_path / "published"
    public.mkdir()

    class HeldPublisher(StaticDirectoryPublisher):
        def publish(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["release_id"])
            entered.set()
            assert release.wait(5)
            return super().publish(*args, **kwargs)

    publisher = HeldPublisher(public, "https://published.example/")
    first = workflow(tmp_path / "private", publisher=publisher)
    second = workflow(first.root, publisher=publisher)
    first.initialize()
    second.initialize()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        data = await built(first)
        original = WebsiteReview(
            expected_version=data["version"], build_digest=data["build"]["digest"]
        )
        approval = first.prepare_publication("owner-a", data["id"], original)
        with pytest.raises(WebsiteConflict):
            second.prepare_publication("owner-a", data["id"], original)
        with pytest.raises(KeyError):
            second.prepare_publication("other-owner", data["id"], original)
        request = WebsitePublish(
            expected_version=approval["expected_version"],
            build_digest=approval["build_digest"],
            approval_token=approval["approval_token"],
            confirm_publication=True,
        )
        pending = asyncio.create_task(first.publish("owner-a", data["id"], request))
        assert await asyncio.to_thread(entered.wait, 5)
        assert second.reconcile() == 0
        current = second.store.get(data["id"], "owner-a")
        assert current["status"] == "publishing"
        with pytest.raises(WebsiteConflict):
            second.prepare_publication(
                "owner-a",
                data["id"],
                original.model_copy(update={"expected_version": current["version"]}),
            )
        with pytest.raises(WebsiteConflict):
            second.command("owner-a", data["id"], capture_request(current, "during-publish"))
        with pytest.raises(WebsiteConflict):
            await second.publish("owner-a", data["id"], request)
        assert len(calls) == 1
        release.set()
        published = await asyncio.wait_for(pending, 5)
        assert published["status"] == "published"
        assert published["publication"]["digest"] == request.build_digest
        assert len(list(public.glob("*/index.html"))) == 1
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await second.close()
        await first.close()


async def test_shutdown_before_task_entry_recovers_acceptance_without_running(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class Immediate(WebsiteWorkflow):
        async def _capture(self, data: dict[str, Any]) -> None:
            calls.append(data["id"])
            data["status"] = "captured"

    first = workflow(tmp_path / "private", cls=Immediate)
    second = workflow(first.root, cls=Immediate)
    first.initialize()
    second.initialize()
    try:
        data = create(first, "before-entry")
        first.command("owner-a", data["id"], capture_request(data))
        # No event-loop yield occurs between scheduling and shutdown.
        await first.close()
        observed = second.store.get(data["id"], "owner-a")
        assert observed["status"] == "interrupted"
        assert calls == []
        second.command("owner-a", data["id"], capture_request(observed, "explicit-retry"))
        await drain(second)
        assert calls == [data["id"]]
    finally:
        await first.close()
        await second.close()


async def test_shared_lifetime_excludes_old_exclusive_owner_until_all_peers_close(
    tmp_path: Path,
) -> None:
    first = workflow(tmp_path / "private")
    second = workflow(first.root)
    first.initialize()
    second.initialize()

    async def probe() -> int:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import fcntl,sys; f=open(sys.argv[1],'a+'); "
            "\ntry: fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)"
            "\nexcept BlockingIOError: sys.exit(17)",
            str(first.root / ".instance.lock"),
        )
        return await asyncio.wait_for(process.wait(), 5)

    try:
        assert await probe() == 17
        await first.close()
        assert await probe() == 17, "closing one peer released the remaining peer's protection"
        await second.close()
        assert await probe() == 0
    finally:
        await first.close()
        await second.close()


async def test_old_exclusive_owner_prevents_new_instance_schema_or_recovery(tmp_path: Path) -> None:
    service = workflow(tmp_path / "private")
    service.root.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import fcntl,sys,time; f=open(sys.argv[1],'a+'); "
        "fcntl.flock(f.fileno(),fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)",
        str(service.root / ".instance.lock"),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
        with pytest.raises(RuntimeError, match="website_workflow_instance_already_running"):
            service.initialize()
        assert not service.store.db.exists(), "schema writes happened before compatibility locking"
        await stop_child(process)
        service.initialize()
        assert service.store.db.exists()
    finally:
        await stop_child(process)
        await service.close()


PUBLICATION_CHILD = r"""
import asyncio, json, sys, time
from pathlib import Path
from app.services.website_branding import InfographicArtistClient
from app.services.website_publisher import StaticDirectoryPublisher
from app.services.website_workflow import WebsiteWorkflow
from app.services.website_workflow_contracts import WebsiteReview, WebsitePublish

async def main():
    root, signals, project_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    class HeldPublisher(StaticDirectoryPublisher):
        def publish(self, *args, **kwargs):
            (signals / 'publish-entered').touch()
            while not (signals / 'release').exists():
                time.sleep(0.01)
            return super().publish(*args, **kwargs)
    service = WebsiteWorkflow(root, brand_client=InfographicArtistClient(None),
                 publisher=HeldPublisher(root.parent / 'published', 'https://published.example/'))
    service.initialize()
    data = service.store.get(project_id, 'owner-a')
    approval = service.prepare_publication('owner-a', project_id, WebsiteReview(
                         expected_version=data['version'], build_digest=data['build']['digest']))
    task = asyncio.create_task(service.publish('owner-a', project_id, WebsitePublish(
                         expected_version=approval['expected_version'],
                         build_digest=approval['build_digest'],
                         approval_token=approval['approval_token'], confirm_publication=True)))
    while not (signals / 'publish-entered').exists():
        await asyncio.sleep(0.01)
    print(json.dumps(service.public(service.store.get(project_id, 'owner-a'))), flush=True)
    await task
    await service.close()
asyncio.run(main())
"""


async def test_abrupt_publication_owner_loss_never_republishes(tmp_path: Path) -> None:
    initial = workflow(tmp_path / "private")
    initial.initialize()
    data = await built(initial)
    artifact = initial.root / data["build_file"]
    original = artifact.read_bytes()
    await initial.close()
    signals = tmp_path / "signals"
    signals.mkdir()

    class RecoverOnly(StaticDirectoryPublisher):
        def publish(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("recovery must never execute a publication")

    peer = workflow(
        initial.root,
        publisher=RecoverOnly(tmp_path / "published", "https://published.example/"),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        PUBLICATION_CHILD,
        str(initial.root),
        str(signals),
        data["id"],
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, "child publication did not enter the real asynchronous publication path"
        active = json.loads(line)
        assert active["status"] == "publishing"
        peer.initialize()
        assert peer.reconcile() == 0
        assert peer.store.get(data["id"], "owner-a")["version"] == active["version"]
        await stop_child(process)
        assert peer.reconcile() == 1
        observed = peer.store.get(data["id"], "owner-a")
        assert observed["status"] == "interrupted"
        assert observed["approval"] is None and observed["publication"] is None
        assert peer.reconcile() == 0
        assert not peer.tasks and not peer.publications
        assert not list((tmp_path / "published").glob("*/index.html"))
        assert artifact.read_bytes() == original
    finally:
        await stop_child(process)
        await peer.close()


async def test_busy_publication_recovery_preserves_intent_until_receipt_can_be_read(
    tmp_path: Path,
) -> None:
    first = workflow(tmp_path / "private")
    first.initialize()
    second: WebsiteWorkflow | None = None
    try:
        data = await built(first)
        build = WebsiteBuild.model_validate(first._read(data["build_file"]))
        release_id = f"{data['id']}-{build.digest[:16]}"
        intent = {
            "digest": build.digest,
            "release_id": release_id,
            "target": first.publisher.public_base_url,
            "root": str(first.publisher.root.resolve()),
        }
        data.update(status="publishing", approval=None, publication_intent=intent)
        data = first.store.save(data, data["version"])
        receipt = first.publisher.publish(
            build, expected_digest=build.digest, release_id=release_id
        )
        public = first.publisher.root
        before = {
            str(p.relative_to(public)): p.read_bytes() for p in public.rglob("*") if p.is_file()
        }

        class RecoverOnly(StaticDirectoryPublisher):
            def publish(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("a matching release must only be read")

        second = workflow(
            first.root, publisher=RecoverOnly(public, first.publisher.public_base_url)
        )
        with (public / ".publish.lock").open("r+") as busy:
            fcntl.flock(busy.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            second.initialize()
            assert second.reconcile() == 0
            observed = second.store.get(data["id"], "owner-a")
            assert observed["status"] == "publishing"
            assert observed["version"] == data["version"]
            assert observed["publication_intent"] == intent
        assert second.reconcile() == 1
        observed = second.store.get(data["id"], "owner-a")
        assert observed["status"] == "published"
        assert observed["publication"] == receipt
        assert second.reconcile() == 0
        after = {
            str(p.relative_to(public)): p.read_bytes() for p in public.rglob("*") if p.is_file()
        }
        assert before == after
    finally:
        if second is not None:
            await second.close()
        await first.close()
