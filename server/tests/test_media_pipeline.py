"""Real binary, lease and ownership proofs for the media vertical slice."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import wave
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from PIL import Image
from test_goal_api import _goal_payload, _plan
from test_planner_provider import _graph_wire_proposal
from test_worker_protocol import register

from app.services.evaluator_provider import NoopEvaluatorProvider
from app.services.media_contracts import MAX_MEDIA_BYTES, MediaArtifact, media_payload
from app.services.media_store import media_root
from app.services.planner_provider import UbuntuSwarmPlannerProvider
from app.services.swarm_contracts import SwarmPlanProposal

IMAGE = {"prompt": "Un paysage bleu", "width": 512, "height": 512, "steps": 4, "seed": 42}
CHROMA = {**IMAGE, "model_profile": "chroma1-hd-q4", "steps": 40}
AUDIO = {
    "text": "Bonjour, voici la voix française.",
    "language": "fr-FR",
    "voice": "ff_siwis",
    "max_duration_seconds": 2,
}


def png(size=(512, 512), color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def wav(*, rate=24000, channels=1, duration=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"\0" * (rate * duration * channels * width))
    return buffer.getvalue()


def start_media(client, paired_headers, test_app, *, skill="image.generate", arguments=None):
    test_app.state.goal_manager.evaluator = NoopEvaluatorProvider()
    test_app.state.goal_manager.project_applications.context = test_app.state.project_context
    args = deepcopy(
        arguments if arguments is not None else IMAGE if skill == "image.generate" else AUDIO
    )
    agent = register(client, paired_headers, "media", [skill])
    objective = (
        "Produire une image bleue et conserver cette exigence précise."
        if skill == "image.generate"
        else "Lire exactement la phrase française demandée."
    )
    request = _goal_payload(objective, autonomy_profile="manual")
    request["completion_criteria"] = [
        "Livrer un fichier vérifié au propriétaire",
        "Préserver la consigne originale",
    ]
    created = client.post("/goals", headers=paired_headers, json=request)
    assert created.status_code == 201, created.text
    goal_id = created.json()["goal"]["id"]
    plan = _plan(objective, temporary_id="media")
    plan["completion_criteria"] = request["completion_criteria"]
    plan["nodes"][0].update(required_skill=skill, worker_arguments=args)
    started = client.post(
        f"/goals/{goal_id}/start",
        headers=paired_headers,
        json={"plan_proposal": plan, "planner_source": "manual"},
    )
    assert started.status_code == 200, started.text
    auth = {"Authorization": f"Bearer {agent['credential']}"}
    claimed = client.post(f"/agents/{agent['id']}/claim", headers=auth, json={})
    assert claimed.status_code == 200 and claimed.json(), claimed.text
    job = claimed.json()
    assert job["required_skill"] == skill
    assert {k: v for k, v in job["payload"].items() if k != "context"} == args
    context = job["payload"]["context"]
    assert context["goal_id"] == goal_id
    assert context["objective"] == objective
    assert context["completion_criteria"] == request["completion_criteria"]
    assert context["durable_context"] is not None
    return goal_id, agent, job, auth


def upload(client, agent, job, auth, data, mime="image/png", **overrides):
    headers = {
        **auth,
        "Content-Type": mime,
        "X-Claim-Token": job["claim_token"],
        "X-Lease-Id": job["lease_id"],
        "X-Lease-Generation": str(job["lease_generation"]),
        "X-Artifact-SHA256": hashlib.sha256(data).hexdigest(),
        **overrides,
    }
    return client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/media", headers=headers, content=data
    )


def finish(client, agent, job, auth, ref):
    return client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={
            "claim_token": job["claim_token"],
            "lease_id": job["lease_id"],
            "lease_generation": job["lease_generation"],
            "status": "completed",
            "result": {"schema_version": "1.0", "content_trust": "untrusted", "artifact": ref},
        },
    )


def other_device(client):
    from conftest import OPERATOR_TOKEN

    code = client.post(
        "/pairing/code", headers={"X-Mongars-Operator-Token": OPERATOR_TOKEN}
    ).json()["code"]
    candidate = client.post(
        "/pairing/complete", json={"code": code, "device_id": "other-phone", "name": "other"}
    ).json()
    auth = {"Authorization": f"Bearer {candidate['candidate_token']}"}
    assert (
        client.post(
            "/pairing/finalize",
            headers=auth,
            json={"pairing_id": candidate["pairing_id"], "device_id": candidate["device_id"]},
        ).status_code
        == 200
    )
    assert client.get("/sync/bootstrap", headers=auth).status_code == 200
    return auth


@pytest.mark.parametrize(
    "skill,args",
    [("image.generate", IMAGE), ("image.generate", CHROMA), ("audio.synthesize", AUDIO)],
)
def test_media_real_binary_cycle_preserves_requirements_owner_and_idempotence(
    client, paired_headers, test_app, skill, args
):
    goal_id, agent, job, auth = start_media(
        client, paired_headers, test_app, skill=skill, arguments=args
    )
    data, mime = (png(), "image/png") if skill == "image.generate" else (wav(), "audio/wav")
    assert client.get(f"/goals/{goal_id}/media", headers=paired_headers).json() == {"artifacts": []}
    uploaded = upload(client, agent, job, auth, data, mime)
    assert uploaded.status_code == 200, uploaded.text
    ref = uploaded.json()
    assert MediaArtifact.model_validate(ref).model_dump() == ref
    assert ref["sha256"] == hashlib.sha256(data).hexdigest()
    assert ref["size_bytes"] == len(data)
    assert ref["goal_id"] == goal_id and ref["job_id"] == job["id"]
    assert upload(client, agent, job, auth, data, mime).json() == ref
    # Upload alone must not publish a successful result.
    assert client.get(f"/goals/{goal_id}/media", headers=paired_headers).json() == {"artifacts": []}
    accepted = finish(client, agent, job, auth, ref)
    assert accepted.status_code == 200, accepted.text
    assert finish(client, agent, job, auth, ref).status_code == 200
    assert client.get(f"/goals/{goal_id}/media", headers=paired_headers).json() == {
        "artifacts": [ref]
    }
    url = f"/goals/{goal_id}/media/{ref['artifact_id']}"
    served = client.get(url, headers=paired_headers)
    assert served.status_code == 200 and served.content == data
    assert served.headers["content-type"] == mime
    assert served.headers["x-artifact-sha256"] == ref["sha256"]
    assert served.headers["cache-control"] == "private, no-store"
    assert client.get(url).status_code == 401
    outsider = other_device(client)
    assert client.get(url, headers=outsider).status_code == 404
    assert client.get(f"/goals/{goal_id}/media", headers=outsider).status_code == 404
    details = client.get(f"/goals/{goal_id}", headers=paired_headers).json()
    assert details["nodes"][0]["status"] == "completed", details
    root = media_root(test_app.state.settings.db_path)
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
    assert not any(test_app.state.settings.workspace_root.rglob("*.png"))
    assert not any(test_app.state.settings.workspace_root.rglob("*.wav"))


@pytest.mark.parametrize(
    "violation", ["hash", "corrupt", "dimensions", "trailing", "mime", "oversize"]
)
def test_media_rejects_invalid_uploads_without_files(client, paired_headers, test_app, violation):
    _, agent, job, auth = start_media(client, paired_headers, test_app)
    data, mime, headers = png(), "image/png", {}
    if violation == "hash":
        headers["X-Artifact-SHA256"] = "0" * 64
    if violation == "corrupt":
        data = data[:100]
    if violation == "dimensions":
        data = png((1, 1))
    if violation == "trailing":
        data += b"untrusted trailer"
    if violation == "mime":
        mime = "application/octet-stream"
    if violation == "oversize":
        headers["Content-Length"] = str(MAX_MEDIA_BYTES + 1)
    result = upload(client, agent, job, auth, data, mime, **headers)
    assert result.status_code in {422, 413, 415}, result.text
    assert not media_root(test_app.state.settings.db_path).exists()


@pytest.mark.parametrize("violation", ["rate", "channels", "duration", "width", "truncated"])
def test_audio_decodes_pcm_and_enforces_request_duration(
    client, paired_headers, test_app, violation
):
    _, agent, job, auth = start_media(client, paired_headers, test_app, skill="audio.synthesize")
    kwargs = (
        {"rate": 22050}
        if violation == "rate"
        else {"channels": 2}
        if violation == "channels"
        else {"duration": 3}
        if violation == "duration"
        else {"width": 1}
        if violation == "width"
        else {}
    )
    data = wav(**kwargs)
    if violation == "truncated":
        data = data[:-2]
        data = data[:4] + (len(data) - 8).to_bytes(4, "little") + data[8:]
    result = upload(client, agent, job, auth, data, "audio/wav")
    assert result.status_code == 422, result.text
    assert result.headers["x-media-error"] == "invalid_output"
    assert not media_root(test_app.state.settings.db_path).exists()


@pytest.mark.parametrize(
    "violation",
    ["no_auth", "wrong_agent", "token", "lease", "generation", "expired", "cancelled", "revision"],
)
def test_upload_requires_current_lease_and_active_goal(client, paired_headers, test_app, violation):
    goal_id, agent, job, auth = start_media(client, paired_headers, test_app)
    extra = {}
    if violation == "no_auth":
        auth = {}
    if violation == "wrong_agent":
        outsider = register(client, paired_headers, "other-media", ["image.generate"])
        auth = {"Authorization": f"Bearer {outsider['credential']}"}
    if violation == "token":
        extra["X-Claim-Token"] = "wrong-token-with-sufficient-length"
    if violation == "lease":
        extra["X-Lease-Id"] = "lease_" + "f" * 32
    if violation == "generation":
        extra["X-Lease-Generation"] = str(job["lease_generation"] + 1)
    if violation in {"expired", "revision"}:
        with sqlite3.connect(test_app.state.settings.db_path) as db:
            if violation == "expired":
                db.execute(
                    "UPDATE agent_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (job["id"],),
                )
            else:
                db.execute(
                    "UPDATE goal_runs SET conversation_revision=conversation_revision+1 WHERE id=?",
                    (goal_id,),
                )
    if violation == "cancelled":
        assert (
            client.post(f"/goals/{goal_id}/cancel", headers=paired_headers, json={}).status_code
            == 200
        )
    result = upload(client, agent, job, auth, png(), **extra)
    assert result.status_code in {401, 403, 409}, result.text
    assert not media_root(test_app.state.settings.db_path).exists()


def test_media_result_cannot_forge_or_change_reference(client, paired_headers, test_app):
    _, agent, job, auth = start_media(client, paired_headers, test_app)
    data = png()
    ref = {
        "artifact_id": "media_" + "0" * 40,
        "job_id": job["id"],
        "goal_id": "goal_" + "0" * 32,
        "media_type": "image/png",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": 512,
        "height": 512,
        "duration_ms": None,
        "sample_rate": None,
        "channels": None,
    }
    assert finish(client, agent, job, auth, ref).status_code == 409
    ref = upload(client, agent, job, auth, data).json()
    for bad in (
        {**ref, "sha256": "0" * 64},
        {**ref, "job_id": "job_" + "f" * 32},
        {**ref, "path": "/tmp/anything"},
    ):
        assert finish(client, agent, job, auth, bad).status_code == 409
    assert upload(client, agent, job, auth, png(color="red")).status_code == 422
    assert finish(client, agent, job, auth, ref).status_code == 200


def test_upload_recovers_only_byte_exact_crash_orphan(client, paired_headers, test_app):
    _, agent, job, auth = start_media(client, paired_headers, test_app)
    data = png()
    root = media_root(test_app.state.settings.db_path)
    root.mkdir(mode=0o700)
    ident = (
        "media_"
        + hashlib.sha256(f"{job['id']}:{job['lease_generation']}".encode()).hexdigest()[:40]
    )
    blob = root / (ident + ".blob")
    blob.write_bytes(b"different bytes")
    assert upload(client, agent, job, auth, data).status_code == 422
    assert blob.read_bytes() == b"different bytes"
    blob.write_bytes(data)
    blob.chmod(0o600)
    recovered = upload(client, agent, job, auth, data)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["artifact_id"] == ident
    assert len(list(root.iterdir())) == 2
    assert finish(client, agent, job, auth, recovered.json()).status_code == 200


def test_list_does_not_read_blobs_but_download_detects_corruption(
    client, paired_headers, test_app, monkeypatch
):
    import app.services.media_store as module

    goal, agent, job, auth = start_media(client, paired_headers, test_app)
    ref = upload(client, agent, job, auth, png()).json()
    assert finish(client, agent, job, auth, ref).status_code == 200
    read = module._read_regular
    paths = []

    def observed(path, maximum):
        paths.append(path)
        return read(path, maximum)

    monkeypatch.setattr(module, "_read_regular", observed)
    assert client.get(f"/goals/{goal}/media", headers=paired_headers).json() == {"artifacts": [ref]}
    assert paths and all(p.suffix == ".json" for p in paths)
    (media_root(test_app.state.settings.db_path) / (ref["artifact_id"] + ".blob")).write_bytes(
        b"corrupt"
    )
    assert (
        client.get(f"/goals/{goal}/media/{ref['artifact_id']}", headers=paired_headers).status_code
        == 404
    )


@pytest.mark.parametrize(
    "skill,args",
    [("image.generate", IMAGE), ("image.generate", CHROMA), ("audio.synthesize", AUDIO)],
)
def test_planner_media_requires_typed_arguments_and_forbids_forged_context(skill, args):
    proposal = _plan("Media", temporary_id="media")
    proposal["nodes"][0].update(required_skill=skill, worker_arguments=args)
    assert SwarmPlanProposal.model_validate(proposal).nodes[0].worker_arguments == args
    schema = UbuntuSwarmPlannerProvider._response_format("Media", available_skills=[skill])[
        "json_schema"
    ]["schema"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid(_graph_wire_proposal(proposal))
    for changed in (
        {},
        {**args, "context": {}},
        {**args, ("width" if skill == "image.generate" else "max_duration_seconds"): 10000},
    ):
        bad = deepcopy(proposal)
        bad["nodes"][0]["worker_arguments"] = changed
        with pytest.raises(ValueError):
            SwarmPlanProposal.model_validate(bad)
        assert not validator.is_valid(_graph_wire_proposal(bad))
    with pytest.raises(ValueError):
        media_payload(skill, {**args, "context": {}})


@pytest.mark.parametrize(
    "args,accepted",
    [
        (IMAGE, True),
        ({**IMAGE, "model_profile": "sdxl-lightning-4step"}, True),
        ({**IMAGE, "width": 768, "height": 768}, True),
        (CHROMA, True),
        ({**IMAGE, "steps": 40}, False),
        ({**IMAGE, "steps": 3}, False),
        ({**IMAGE, "model_profile": "unknown"}, False),
        ({**CHROMA, "steps": 4}, False),
        ({**CHROMA, "steps": 41}, False),
        ({**CHROMA, "width": 768}, False),
        ({**CHROMA, "height": 768}, False),
        ({**CHROMA, "runtime_path": "/tmp/other-cli"}, False),
        ({**CHROMA, "model_profile": None}, False),
    ],
)
def test_image_profiles_are_consistent_in_public_and_model_wire_contracts(args, accepted):
    proposal = _plan("Image locale", temporary_id="media")
    proposal["nodes"][0].update(required_skill="image.generate", worker_arguments=args)
    schema = UbuntuSwarmPlannerProvider._response_format(
        "Image locale", available_skills=["image.generate"]
    )["json_schema"]["schema"]
    assert Draft202012Validator(schema).is_valid(_graph_wire_proposal(proposal)) is accepted
    if accepted:
        # Preserve legacy payload identity: an omitted profile is not silently rewritten.
        assert SwarmPlanProposal.model_validate(proposal).nodes[0].worker_arguments == args
        assert media_payload("image.generate", args) == args
    else:
        with pytest.raises(ValueError):
            SwarmPlanProposal.model_validate(proposal)
        with pytest.raises(ValueError):
            media_payload("image.generate", args)


@pytest.mark.parametrize("base", [IMAGE, CHROMA])
@pytest.mark.parametrize("field", ["width", "height", "steps", "seed"])
def test_image_payload_rejects_float_numbers_before_forwarding_to_strict_worker(base, field):
    with pytest.raises(ValueError):
        media_payload("image.generate", {**base, field: float(base[field])})


def test_media_full_payload_utf8_limit_never_drops_requirements():
    base = {
        **IMAGE,
        "context": {
            "goal_id": "goal_" + "a" * 32,
            "objective": "objectif",
            "completion_criteria": ["Garder chaque exigence"],
            "step_objective": "",
            "conversation_revision": 0,
            "durable_context": None,
        },
    }
    remaining = 64000 - len(json.dumps(base, ensure_ascii=False, allow_nan=False).encode())
    base["context"]["step_objective"] = "é" * (remaining // 2) + "x" * (remaining % 2)
    assert len(json.dumps(base, ensure_ascii=False, allow_nan=False).encode()) == 64000
    assert media_payload("image.generate", base) == base
    base["context"]["step_objective"] += "é"
    with pytest.raises(ValueError, match="input budget"):
        media_payload("image.generate", base)


@pytest.mark.parametrize(
    "error,result,accepted",
    [
        ("duration_limit", None, True),
        ("storage_limit", None, True),
        ("resource_busy", None, True),
        ("raw secret error text", None, False),
        ("invalid_output", {"base64": "bytes"}, False),
    ],
)
def test_media_failure_is_content_free_closed_code(
    client, paired_headers, test_app, error, result, accepted
):
    _, agent, job, auth = start_media(client, paired_headers, test_app, skill="audio.synthesize")
    response = client.post(
        f"/agents/{agent['id']}/jobs/{job['id']}/result",
        headers=auth,
        json={
            "claim_token": job["claim_token"],
            "lease_id": job["lease_id"],
            "lease_generation": job["lease_generation"],
            "status": "failed",
            "result": result,
            "error": error,
        },
    )
    assert response.status_code == (200 if accepted else 409), response.text
    if accepted:
        with sqlite3.connect(test_app.state.settings.db_path) as db:
            assert db.execute(
                "SELECT result_json,error FROM agent_jobs WHERE id=?", (job["id"],)
            ).fetchone() == (None, error)


def test_media_orphan_symlink_never_followed(client, paired_headers, test_app, tmp_path):
    _, agent, job, auth = start_media(client, paired_headers, test_app)
    root = media_root(test_app.state.settings.db_path)
    root.mkdir(mode=0o700)
    ident = (
        "media_"
        + hashlib.sha256(f"{job['id']}:{job['lease_generation']}".encode()).hexdigest()[:40]
    )
    external = tmp_path / "external"
    external.write_bytes(png())
    (root / (ident + ".blob")).symlink_to(external)
    assert upload(client, agent, job, auth, png()).status_code == 422
    assert external.read_bytes() == png()
    assert not (root / (ident + ".json")).exists()


def test_media_quota_counts_receipt_and_leaves_no_partial_upload(client, paired_headers, test_app):
    _, agent, job, auth = start_media(client, paired_headers, test_app)
    root = media_root(test_app.state.settings.db_path)
    root.mkdir(mode=0o700)
    data = png()
    with (root / "existing.blob").open("wb") as output:
        output.truncate(512 * 1024 * 1024 - len(data))
    result = upload(client, agent, job, auth, data)
    assert result.status_code == 507
    assert result.headers["x-media-error"] == "storage_limit"
    assert [p.name for p in root.iterdir()] == ["existing.blob"]


def test_failed_receipt_write_cleans_own_bytes_and_allows_retry(
    client, paired_headers, test_app, monkeypatch
):
    import app.services.media_store as module

    _, agent, job, auth = start_media(client, paired_headers, test_app)
    original = module.os.replace

    def reject_receipt(source, destination):
        if str(destination).endswith(".json"):
            # The temporary receipt is complete, yet invisible to readers.
            assert source.read_bytes()
            assert not destination.exists()
            raise OSError("simulated interrupted publication")
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", reject_receipt)
    with pytest.raises(OSError, match="simulated"):
        upload(client, agent, job, auth, png())
    assert list(media_root(test_app.state.settings.db_path).iterdir()) == []
    monkeypatch.setattr(module.os, "replace", original)
    assert upload(client, agent, job, auth, png()).status_code == 200


async def test_image_waits_for_gpu_without_spending_attempts_and_audio_cpu_can_run(tmp_path):
    from test_model_resource_integration import (
        LocalPlanner,
        goal,
        manager,
        queued,
        reserve,
        stored_goal,
        worker,
    )

    value = await manager(tmp_path, LocalPlanner())
    model_goal = await goal(value)
    call = await reserve(value, model_goal["id"])
    image = await queued(value, "image.generate", IMAGE)
    audio = await queued(value, "audio.synthesize", AUDIO)
    media_id = await worker(value, ["image.generate", "audio.synthesize"])
    before = await stored_goal(value, model_goal["id"])
    claimed = await value.agent_dispatcher.claim(media_id)
    assert claimed is not None and claimed["id"] == audio["id"]
    assert await value.agent_dispatcher.claim(media_id) is None
    waiting = await value.agent_dispatcher.get_job(image["id"])
    assert waiting["status"] == "queued" and waiting["attempt_count"] == 0
    assert waiting["lease_id"] is None
    assert (await stored_goal(value, model_goal["id"]))["model_call_count"] == before[
        "model_call_count"
    ]
    # Ending only the audio does not free a GPU still reserved by the API.
    await value.agent_dispatcher.submit_result(
        media_id,
        audio["id"],
        claimed["claim_token"],
        status="failed",
        result=None,
        error="runtime_error",
        lease_id=claimed["lease_id"],
        lease_generation=claimed["lease_generation"],
    )
    assert await value.agent_dispatcher.claim(media_id) is None
    assert await value._finish_model_call(call, status="completed")
    admitted = await value.agent_dispatcher.claim(media_id)
    assert admitted is not None and admitted["id"] == image["id"]
    assert admitted["attempt_count"] == 1


@pytest.mark.parametrize(
    "filename", ["agent-card-audio.json", "agent-card-image.json", "agent-card-chroma.json"]
)
def test_media_worker_cards_have_bounded_private_policy(filename):
    from pathlib import Path

    from app.services.agent_card import AgentCardPolicyError, validate_agent_card_manifest

    path = Path(__file__).resolve().parents[2] / "workers" / "media-worker" / filename
    card = json.loads(path.read_text())
    policy = validate_agent_card_manifest(card)
    assert policy.skills in (("image.generate",), ("audio.synthesize",))
    assert policy.policy["filesystem"] == "isolated-media-scratch"
    assert policy.policy["shell"] is False
    assert policy.policy["writes"] is True
    invalid = deepcopy(card)
    invalid["policy"]["shell"] = True
    with pytest.raises(AgentCardPolicyError):
        validate_agent_card_manifest(invalid)
    invalid = deepcopy(card)
    invalid["limits"]["max_operation_seconds"] = 601
    with pytest.raises(AgentCardPolicyError):
        validate_agent_card_manifest(invalid)
