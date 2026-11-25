"""Deterministic verifier for trusted collection bundles, never agent-written truth.

The controller/Harbor sidecar must supply the receipt over a channel inaccessible
to model tools. Hashes detect drift, not forgery by a writer of both bundle files.
No model judge, external API, achievement reconstruction or fake award is used.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .contracts import ContractError, TaskKind, _exact_fields, _require, _unique_object, decode_contract
from .grading import grade_achievements, grade_survival
from .native_state import decode_final, decode_survival_start


RECEIPT_FIELDS = {"attempt_id", "contract_sha256", "snapshot_sha256", "finish_declared",
                  "inputs_closed", "native_frozen", "fresh_profile", "status",
                  "confirmed_violations", "successful_bypasses", "survival_start_sha256",
                  "fairness_sha256", "integrity_sha256"}


def _write(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def verify_bundle(artifacts: Path, contract_path: Path, output: Path) -> dict:
    """Always initialize numeric zero before reading any potentially broken input."""
    artifacts, contract_path, output = Path(artifacts), Path(contract_path), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    _write(output / "reward.json", {"reward": 0.0})
    # Harbor prioritizes this legacy filename over reward.json. The verifier owns
    # its output directory; stale success must not override a current failure.
    (output / "reward.txt").unlink(missing_ok=True)
    contract = None
    try:
        contract_raw = contract_path.read_bytes()
        contract = decode_contract(contract_raw)
        if contract.version in {2, 3}:
            from .gameplay_receipt import verify_gameplay
            result = verify_gameplay(artifacts, contract, contract_raw)
            _write(output / "diagnostics.json", result)
            # Harbor requires a numeric transport reward. Diagnostics carry the
            # nullable score and exclusion; infrastructure is never ranked zero.
            _write(output / "reward.json", {"reward": float(result["reward"] or 0)})
            return result
        # Historical readers are not dependencies of the current verifier image.
        from .fair_play import read_fairness_evidence
        from .score_integrity import read_integrity_evidence
        receipt = json.loads((artifacts / "receipt.json").read_bytes(),
                             object_pairs_hook=_unique_object)
        _exact_fields(receipt, RECEIPT_FIELDS)
        _require(isinstance(receipt["attempt_id"], str) and bool(receipt["attempt_id"]), "attempt_id")
        _require(receipt["contract_sha256"] == hashlib.sha256(contract_raw).hexdigest(),
                 "contract identity")
        raw = (artifacts / "final.json").read_bytes()
        _require(receipt["snapshot_sha256"] == hashlib.sha256(raw).hexdigest(), "snapshot identity")
        for field in ("inputs_closed", "native_frozen", "fresh_profile"):
            _require(receipt[field] is True, field)
        _require(receipt["status"] == "finalized", "collection status")
        _require(type(receipt["finish_declared"]) is bool, "finish declaration")
        for field in ("confirmed_violations", "successful_bypasses"):
            _require(type(receipt[field]) is int and receipt[field] >= 0, field)
        snapshot = decode_final(raw, receipt["attempt_id"])
        _require(snapshot.game_version == contract.game_version and
                  snapshot.game_sha256 == contract.game_sha256, "game identity")
        if contract.task is TaskKind.SURVIVAL and snapshot.has_played:
            start_raw = (artifacts / "survival-start.json").read_bytes()
            _require(receipt["survival_start_sha256"] == hashlib.sha256(start_raw).hexdigest(),
                     "survival start identity")
            decode_survival_start(start_raw, contract.survival, snapshot.attempt_id)
        else:
            _require(receipt["survival_start_sha256"] is None, "unexpected survival start binding")
        if snapshot.reason == "agent_finish":
            _require(receipt["finish_declared"], "explicit finish declaration")
        fairness = read_fairness_evidence(artifacts, snapshot.attempt_id, receipt["fairness_sha256"])
        integrity = read_integrity_evidence(artifacts, snapshot.attempt_id, receipt["integrity_sha256"],
                                             receipt["snapshot_sha256"])
        _require(not (receipt["confirmed_violations"] or receipt["successful_bypasses"]) or
                 fairness["status"] == "failed", "policy counts lack bound confirmed evidence")
        outcome = (grade_achievements(snapshot, contract.eligible_ids, legacy=contract.version == 1)
                   if contract.task is TaskKind.ACHIEVEMENTS
                   else grade_survival(snapshot, contract.survival))
        objective = json.loads(json.dumps(asdict(outcome)))
        if integrity["status"] != "trusted":
            result = {"status": "unassessed", "reason": "untrusted_native_measurement",
                       "reward": 0.0, "play_seconds": snapshot.play_seconds}
        else:
            result = dict(objective)
        result.update(fairness=fairness, objective_outcome=objective, end_reason=snapshot.reason)
        # Cheating observation is a secondary annotation, not a replacement score.
        # Eligibility does not certify fairness; coverage limitations remain visible.
        result["comparison_eligible"] = (integrity["status"] == "trusted" and
                                          fairness["status"] == "no_observed_violation" and
                                          snapshot.reason != "infrastructure_error")
        result.update(integrity=integrity, raw_native_measurement={
            "play_seconds": snapshot.play_seconds, "achieved_ids": list(snapshot.achieved_ids)},
            trusted_play_seconds=snapshot.play_seconds if integrity["status"] == "trusted" else None)
        if contract.task is TaskKind.ACHIEVEMENTS:
            earned = len(set(snapshot.achieved_ids) & set(contract.eligible_ids))
            fraction = earned / len(contract.eligible_ids)
            result["achievement_progress"] = {"earned": earned, "eligible": len(contract.eligible_ids),
                "fraction": fraction,
                "trusted_fraction": fraction if integrity["status"] == "trusted" else None}
        result["attempt_id"] = snapshot.attempt_id
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        result = {"status": "infrastructure_error", "reason": "invalid_or_missing_trusted_evidence",
                    "reward": 0.0, "comparison_eligible": False, "error_type": type(error).__name__, "detail": str(error)}
        if contract is not None and contract.version == 3:
            result.update(contract_version=3, reward=None, rank_key=None)
    _write(output / "diagnostics.json", result)
    _write(output / "reward.json", {"reward": float(result["reward"] or 0)})
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    result = verify_bundle(args.artifacts, args.contract, args.output)
    return 1 if result["status"] == "infrastructure_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
