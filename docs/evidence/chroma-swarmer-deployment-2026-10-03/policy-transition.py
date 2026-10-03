"""One additive image policy transition, with separate immutable evidence."""
import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

OLD = "30eed47e5b7f5c47d56e93c6b6627523945ddaf6a3b999e228fabf713b6d7eb8"
NEW = "e14eae2f904d447cc1a407d7f371fbd209e7ef74bcabfdbb71bbdbbca9a68303"
OLD_DIGEST = "sha256:4b527c1f45f4a9a6fcea7bc533bf3a810a30601fd118b2aa95b676c8de034373"
NEW_DIGEST = "sha256:9f96a9c90e176aeaa1b023250d13f077dbc10a0e546c0659f1680bc7f98c3584"
HOME = Path.home()
DB = HOME / ".local/state/swarmer-control-plane/mongars.db"
POLICY = HOME / ".config/swarmer-control-plane/permissions-swift.yaml"

def require(value, message):
    if not value:
        raise RuntimeError(message)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def row(db):
    cursor = db.execute("SELECT * FROM worker_skill_policy_state")
    columns = [x[0] for x in cursor.description]
    rows = cursor.fetchall()
    require(len(rows) == 1, "policy row count changed")
    return dict(zip(columns, rows[0]))

def write(path, value):
    with path.open("x") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())

def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--mode", choices=["apply", "verify"], required=True)
    args = p.parse_args()
    work = args.work.resolve(strict=True)
    gate = load("policy_gate", work / "admission.py")
    release = load("policy_release", work / "guarded_release.py")
    evidence = work / "image-policy-transition"
    require(os.getuid() == 1000, "operator identity mismatch")
    require(sha(args.candidate) == NEW, "candidate policy changed")
    require(not POLICY.is_symlink(), "policy symlink refused")
    if args.mode == "apply":
        require(sha(POLICY) == OLD, "current policy differs from reviewed predecessor")
        evidence.mkdir(mode=0o700)
        with closing(gate.db_read()) as db, closing(sqlite3.connect(evidence / "before.db")) as backup:
            db.backup(backup)
        with closing(sqlite3.connect(evidence / "before.db")) as db:
            db.execute("BEGIN")
            gate.idle(db)
            prior = row(db)
            require(prior["epoch"] == 7 and prior["rules_digest"] == OLD_DIGEST, "policy epoch changed")
            before = gate.protected_snapshot(db)
            write(evidence / "before.json", {"protected": before, "policy": prior})
        (evidence / "permissions-before.yaml").write_bytes(POLICY.read_bytes())
        write(evidence / "intent.json", {"old_file_sha256": OLD, "new_file_sha256": NEW,
                                         "before_db_sha256": sha(evidence / "before.db")})
        temporary = POLICY.with_name(POLICY.name + ".chroma-pending")
        with temporary.open("xb") as output:
            output.write(args.candidate.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, POLICY)
    require(sha(POLICY) == NEW, "candidate policy is not active on disk")
    for _ in range(31):
        with closing(gate.db_read()) as db:
            if row(db)["epoch"] == 8:
                break
        time.sleep(1)
    prior = json.loads((evidence / "before.json").read_text())
    with closing(gate.db_read()) as current, closing(sqlite3.connect(evidence / "before.db")) as old:
        old.execute("BEGIN")
        require(current.in_transaction, "current snapshot not consistent")
        actual = row(current)
        require(actual["singleton_id"] == prior["policy"]["singleton_id"], "policy identity changed")
        require(actual["epoch"] == 8 and actual["rules_digest"] == NEW_DIGEST, "policy reload not confirmed")
        previous_rules = json.loads(prior["policy"]["rules_json"])
        rules = json.loads(actual["rules_json"])
        require(isinstance(rules, dict) and isinstance(previous_rules, dict), "unexpected policy shape")
        require(set(rules) - set(previous_rules) == {"image.generate"}, "policy additions differ")
        require(all(rules.get(k) == v for k, v in previous_rules.items()), "existing rule changed")
        require("audio.synthesize" not in rules, "audio unexpectedly enabled")
        observed = gate.protected_snapshot(current)
        require(set(observed) == set(prior["protected"]), "protected inventory changed")
        changed = {k for k in observed if observed[k] != prior["protected"][k]}
        require(changed <= {"worker_skill_policy_state", "goal_contexts", "devices"}, "unexpected protected-data change")
        contexts = gate.verify_known_context_append(current, prior["protected"]["goal_contexts"], old)
        old_device = release.device_presence_projection(old)
        new_device = release.device_presence_projection(current)
        require(old_device["stable"] == new_device["stable"] and old_device["schema"] == new_device["schema"], "device identity changed")
        proof = {"enabled_skill": "image.generate", "epoch_before": 7, "epoch_after": 8,
                 "rules_digest": NEW_DIGEST, "previous_rules_preserved": True,
                 "audio_enabled": False, "protected_fingerprints": len(observed),
                 "changed_tables": sorted(changed), "contexts": contexts,
                 "device_identity_preserved": True, "new_policy_file_sha256": NEW}
    if not (evidence / "verified.json").exists():
        write(evidence / "verified.json", proof)
    print(json.dumps(proof, sort_keys=True))

if __name__ == "__main__":
    main()
