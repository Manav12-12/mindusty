"""Protected result handoff for v2 history and v3; no observational-era receipts."""

from dataclasses import asdict
import hashlib
import json
import re

from .contracts import TaskKind, _exact_fields, _require, _unique_object
from .grading import grade_achievements, grade_survival, grade_campaign
from .native_state import decode_final, decode_survival_start


PROFILE = "preventive-v2"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_isolation(data, *, expected_image=None):
    _exact_fields(data, {"profile", "game_image", "game_container", "network_mode", "mounts", "user"})
    _require(data["profile"] == PROFILE and data["network_mode"] == "none" and
        data["mounts"] == [] and data["user"] == "1000:1000", "preventive game boundary")
    _require(isinstance(data["game_image"], str) and
        re.fullmatch(r"sha256:[a-f0-9]{64}", data["game_image"]) is not None and
        isinstance(data["game_container"], str) and
        re.fullmatch(r"[a-f0-9]{64}", data["game_container"]) is not None, "game instance identity")
    _require(expected_image is None or data["game_image"] == expected_image, "accepted game image")


def read_bundle(artifacts, contract, contract_raw):
    receipt = json.loads((artifacts / "receipt.json").read_bytes(), object_pairs_hook=_unique_object)
    _exact_fields(receipt, {"benchmark_profile", "attempt_id", "contract_sha256", "snapshot_sha256",
        "isolation_sha256", "survival_start_sha256", "finish_declared", "inputs_closed", "native_frozen",
        "fresh_profile", "status"})
    _require(receipt["benchmark_profile"] == PROFILE and contract.version in {2, 3}, "preventive receipt revision")
    _require(receipt["contract_sha256"] == sha(contract_raw), "contract identity")
    _require(receipt["status"] == "finalized" and type(receipt["finish_declared"]) is bool and
        all(receipt[k] is True for k in ("inputs_closed", "native_frozen", "fresh_profile")), "frozen attempt")
    isolation_raw = (artifacts / "isolation.json").read_bytes()
    _require(sha(isolation_raw) == receipt["isolation_sha256"], "protected isolation handoff")
    validate_isolation(json.loads(isolation_raw, object_pairs_hook=_unique_object))
    raw = (artifacts / "final.json").read_bytes()
    _require(sha(raw) == receipt["snapshot_sha256"], "snapshot identity")
    snapshot = decode_final(raw, receipt["attempt_id"])
    _require(snapshot.format_version == (2 if contract.version == 3 else 1),
             "contract/native milestone revision pairing")
    _require(snapshot.game_sha256 == contract.game_sha256 and snapshot.game_version == contract.game_version,
        "game identity")
    _require(snapshot.reason != "agent_finish" or receipt["finish_declared"], "finish declaration")
    if contract.task is TaskKind.SURVIVAL and snapshot.has_played:
        start = (artifacts / "survival-start.json").read_bytes()
        _require(sha(start) == receipt["survival_start_sha256"], "survival start binding")
        decode_survival_start(start, contract.survival, snapshot.attempt_id)
    else:
        _require(receipt["survival_start_sha256"] is None, "unexpected start")
    return receipt, snapshot


def verify_gameplay(artifacts, contract, contract_raw):
    receipt, snapshot = read_bundle(artifacts, contract, contract_raw)
    if contract.version == 3:
        result = grade_campaign(snapshot, contract.eligible_ids)
        result.update(benchmark_profile=PROFILE, attempt_id=snapshot.attempt_id,
            integrity={"status": "trusted", "basis": "protected-controller-collection-and-preventive-isolation"})
        return result
    outcome = (grade_achievements(snapshot, contract.eligible_ids) if contract.task is TaskKind.ACHIEVEMENTS
        else grade_survival(snapshot, contract.survival))
    result = asdict(outcome)
    eligible = snapshot.reason not in {"infrastructure_error", "policy_violation"}
    result.update(benchmark_profile=PROFILE, attempt_id=snapshot.attempt_id,
        end_reason=snapshot.reason, comparison_eligible=eligible,
        integrity={"status": "trusted", "basis": "protected-controller-collection-and-preventive-isolation"},
        raw_native_measurement={"play_seconds": snapshot.play_seconds, "achieved_ids": list(snapshot.achieved_ids)},
        trusted_play_seconds=snapshot.play_seconds,
        objective_outcome=asdict(outcome))
    if contract.task is TaskKind.ACHIEVEMENTS:
        earned = len(set(snapshot.achieved_ids) & set(contract.eligible_ids))
        fraction = earned / len(contract.eligible_ids)
        result["achievement_progress"] = {"earned": earned, "eligible": len(contract.eligible_ids),
            "fraction": fraction, "trusted_fraction": fraction}
    return result
