"""Score frozen evidence; do not recreate native achievement or combat predicates."""

from dataclasses import asdict
import json

from .contracts import NativeState, CampaignState, Outcome, SurvivalContract, _require
from .native_state import decode_final


def _validate(snapshot: NativeState) -> None:
    _require(isinstance(snapshot, NativeState), "snapshot")
    # Preserve the same fail-closed validation even for a direct Python caller.
    decode_final(json.dumps(asdict(snapshot)).encode(), snapshot.attempt_id)


def grade_achievements(snapshot: NativeState, eligible_ids: tuple[str, ...], *, legacy=False) -> Outcome:
    """The caller supplies the frozen audited list, never a model-selected subset."""
    _validate(snapshot)
    _require(isinstance(eligible_ids, (tuple, list)) and bool(eligible_ids) and
             all(isinstance(i, str) and i in snapshot.known_ids for i in eligible_ids),
             "eligible_ids")
    _require(len(set(eligible_ids)) == len(eligible_ids), "eligible_ids")
    missing = tuple(sorted(set(eligible_ids) - set(snapshot.achieved_ids)))
    reward = (len(eligible_ids) - len(missing)) / len(eligible_ids)
    if legacy:
        reward = float(not missing and snapshot.reason == "agent_finish")
    if snapshot.reason != "agent_finish":
        return Outcome(0.0 if snapshot.reason in {"infrastructure_error", "policy_violation"} else reward,
                       snapshot.reason, snapshot.reason, snapshot.play_seconds,
                        missing_ids=missing)
    if missing:
        return Outcome(reward, "incomplete", snapshot.reason, snapshot.play_seconds,
                       missing_ids=missing)
    return Outcome(1.0, "success", snapshot.reason, snapshot.play_seconds,
                   completion_seconds=snapshot.play_seconds)


def grade_survival(snapshot: NativeState, contract: SurvivalContract) -> Outcome:
    """Duration scoring only; approved start/scenario identity is a lifecycle gate."""
    _validate(snapshot)
    _require(isinstance(contract, SurvivalContract) and
             contract.defeat == "last-team-core-lost", "survival contract")
    _require(snapshot.team_id == contract.team_id, "team_id")
    reason = snapshot.reason
    if reason in {"policy_violation", "infrastructure_error", "stagnation"}:
        return Outcome(0.0, reason, reason, snapshot.play_seconds)
    if reason == "natural_defeat":
        _require(snapshot.has_played and snapshot.game_over and snapshot.cores_alive == 0,
                 "native defeat")
        return Outcome(float(snapshot.play_seconds), "defeated", reason, snapshot.play_seconds)
    if not snapshot.has_played:
        _require(snapshot.play_seconds == 0, "unstarted clock")
        return Outcome(0.0, "not_started", reason, snapshot.play_seconds)
    _require(not snapshot.game_over and snapshot.cores_alive > 0, "alive stop")
    return Outcome(float(snapshot.play_seconds), "censored", reason, snapshot.play_seconds,
                    censored=True)


def grade_campaign(snapshot: CampaignState, eligible_ids: tuple[str, ...]) -> dict:
    """Progress first, then first-earned active time; infrastructure is not a zero."""
    _validate(snapshot)
    _require(isinstance(snapshot, CampaignState), "native campaign milestones")
    _require(isinstance(eligible_ids, (tuple, list)) and bool(eligible_ids) and
        len(set(eligible_ids)) == len(eligible_ids) and set(eligible_ids) <= set(snapshot.known_ids),
        "eligible campaign achievements")
    earned = set(snapshot.achieved_ids) & set(eligible_ids)
    missing = sorted(set(eligible_ids) - earned)
    milestone_time = max((snapshot.achievement_times[key] for key in earned), default=0)
    eligible = snapshot.reason not in {"infrastructure_error", "policy_violation"}
    completed = not missing and eligible
    return {'contract_version': 3, 'comparison_policy': 'available-subscription-v1',
        'status': ('success' if completed else 'incomplete') if eligible else snapshot.reason,
        'reward': len(earned) / len(eligible_ids) if eligible else None,
        'comparison_eligible': eligible, 'earned': len(earned), 'eligible': len(eligible_ids),
        'missing_ids': missing, 'play_seconds': snapshot.play_seconds,
        'achievement_times': {key: snapshot.achievement_times[key] for key in sorted(earned)},
        'completion_seconds': milestone_time if completed else None,
        'rank_key': [-len(earned), milestone_time] if eligible else None, 'end_reason': snapshot.reason,
        'reason': snapshot.reason,
        'achievement_progress': {'earned': len(earned), 'eligible': len(eligible_ids),
            'fraction': len(earned) / len(eligible_ids),
            'trusted_fraction': len(earned) / len(eligible_ids) if eligible else None}}
