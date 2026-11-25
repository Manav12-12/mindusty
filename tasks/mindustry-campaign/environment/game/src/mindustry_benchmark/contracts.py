"""Fail-closed task configuration. Unit fixtures never constitute approved rules."""

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
import math


class ContractError(ValueError):
    """A contract is missing, ambiguous, unsafe, or not yet approved."""


class TaskKind(StrEnum):
    CAMPAIGN = "mindustry-campaign"
    ACHIEVEMENTS = "offline-achievements"
    SURVIVAL = "survival"


ACTIVE_TASKS = (TaskKind.CAMPAIGN,)


SAFE_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789") | frozenset({
    "Escape", "Return", "space", "Tab", "BackSpace", "Delete", "F8",
    "Left", "Right", "Up", "Down", "Home", "End",
    "Shift_L", "Control_L", "minus", "equal", "comma", "period", "slash",
    "semicolon", "apostrophe", "bracketleft", "bracketright", "backslash",
})


@dataclass(frozen=True)
class InputAction:
    """Immutable validated fixed operation; no script, path, command or tool string."""

    kind: str
    arguments: tuple[tuple[str, object], ...]

    @classmethod
    def from_dict(cls, data: dict, *, width: int, height: int) -> "InputAction":
        _require(isinstance(data, dict), "action")
        kind = data.get("kind")
        fields = {
            "click": {"x", "y", "button"},
            "drag": {"x", "y", "to_x", "to_y", "button", "duration"},
            "key_down": {"key"}, "key_up": {"key"}, "key_press": {"key"},
            "mouse_move": {"x", "y"}, "mouse_down": {"button"}, "mouse_up": {"button"},
            "key_hold": {"keys", "duration"},
            "scroll": {"direction", "steps"}, "wait": {"duration"},
        }
        _require(isinstance(kind, str) and kind in fields, "action.kind")
        _exact_fields(data, fields[kind] | {"kind"})
        for key, limit in (("x", width), ("to_x", width), ("y", height), ("to_y", height)):
            if key in data:
                _require(type(data[key]) is int and 0 <= data[key] < limit, f"action.{key}")
        if "button" in data:
            _require(data["button"] in ("left", "right", "middle"), "action.button")
        if "key" in data:
            _require(isinstance(data["key"], str) and data["key"] in SAFE_KEYS, "action.key")
        if "keys" in data:
            keys = data['keys']
            _require(isinstance(keys, list) and 1 <= len(keys) <= len(SAFE_KEYS) and
                all(isinstance(key, str) and key in SAFE_KEYS for key in keys) and
                len(set(keys)) == len(keys), 'action.keys')
        if "duration" in data:
            duration = data["duration"]
            _require(type(duration) in (int, float) and 0 <= duration <= 30 and
                     math.isfinite(duration), "action.duration")
        if "direction" in data:
            _require(data["direction"] in ("up", "down"), "action.direction")
            _require(type(data["steps"]) is int and 1 <= data["steps"] <= 10, "action.steps")
        return cls(kind, tuple((k, tuple(v) if k == 'keys' else v)
                              for k, v in data.items() if k != "kind"))

    def to_dict(self) -> dict:
        return {"kind": self.kind, **{key: list(value) if key == 'keys' else value
                                     for key, value in self.arguments}}


@dataclass(frozen=True)
class Observation:
    observation_id: str
    image_bytes: bytes
    width: int
    height: int


@dataclass(frozen=True)
class NativeState:
    format_version: int
    attempt_id: str
    game_version: str
    game_sha256: str
    achieved_ids: tuple[str, ...]
    known_ids: tuple[str, ...]
    play_seconds: float
    finalized: bool
    reason: str
    team_id: int
    cores_alive: int
    game_over: bool
    has_played: bool


@dataclass(frozen=True)
class CampaignState(NativeState):
    achievement_times: dict[str, float]


@dataclass(frozen=True)
class ClockSample:
    attempt_id: str
    play_seconds: float
    finalized: bool
    has_played: bool
    protected_work: bool


@dataclass(frozen=True)
class Outcome:
    reward: float
    status: str
    reason: str
    play_seconds: float
    completion_seconds: float | None = None
    missing_ids: tuple[str, ...] = ()
    censored: bool = False


@dataclass(frozen=True)
class SurvivalContract:
    map_id: str
    map_sha256: str
    seed: int | None  # null explicitly preserves native randomness; not a fixed-seed claim
    team_id: int
    start_preset: str
    defeat: str
    allow_sector_exit: bool


def native_survival_start(start: SurvivalContract) -> str:
    """Select only the implemented native bootstrap, never approximate rules."""
    approved = SurvivalContract(
        "maps/default/maze.msav",
        "a7b4443a153c1e0100ec0ab23d2536272f9dfa864fea3c340110f329523b51fb",
        None, 1, "unchanged-native-survival", "last-team-core-lost", False)
    _require(start == approved, "supported survival start")
    return "maze-native-v160.7"


@dataclass(frozen=True)
class TaskContract:
    version: int
    task: TaskKind
    policy_approved: bool
    game_version: str
    game_sha256: str
    harbor_version: str
    fresh_profile: bool
    clock_policy: str
    speed: int
    eligible_ids: tuple[str, ...]
    survival: SurvivalContract | None


@dataclass(frozen=True)
class CampaignContract:
    version: int
    task: TaskKind
    policy_approved: bool
    game_version: str
    game_sha256: str
    harbor_version: str
    fresh_profile: bool
    clock_policy: str
    speed: int
    eligible_ids: tuple[str, ...]


def _require(condition: bool, field: str) -> None:
    if not condition:
        raise ContractError(f"Invalid or unresolved {field}")


def _exact_fields(data: dict, fields: set[str]) -> None:
    _require(isinstance(data, dict), "object")
    missing = fields - data.keys()
    extra = data.keys() - fields
    if missing or extra:
        raise ContractError(f"Missing fields: {sorted(missing)}; unknown fields: {sorted(extra)}")


def _checksum(value: object, field: str) -> None:
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None, field)


def _nonempty(value: object, field: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), field)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_contract(path: Path) -> TaskContract:
    """Read an explicitly approved contract; never infer unfinished production rules."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContractError(f"Cannot read contract JSON: {error}") from error
    return decode_contract(raw)


def decode_contract(raw: bytes) -> TaskContract:
    """Validate the exact immutable revision bound to an attempt and its receipt."""
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"Cannot read contract JSON: {error}") from error
    _require(isinstance(data, dict) and type(data.get("version")) is int and data["version"] in {1, 2, 3}, "version")
    campaign = data["version"] == 3
    _require(not campaign or data.get("task") == TaskKind.CAMPAIGN.value, "version/task pairing")
    _exact_fields(data, set(CampaignContract.__dataclass_fields__ if campaign else TaskContract.__dataclass_fields__))
    try:
        task = TaskKind(data["task"])
    except (ValueError, TypeError) as error:
        raise ContractError("Invalid task") from error
    _require(data["policy_approved"] is True, "policy_approved")
    _require(data["fresh_profile"] is True, "fresh_profile")
    _require(type(data["speed"]) is int and data["speed"] == 1, "speed")
    _require(isinstance(data["game_version"], str) and
             re.fullmatch(r"v\d+\.\d+(?:\.\d+)?", data["game_version"]) is not None, "game_version")
    _require(isinstance(data["harbor_version"], str) and
             re.fullmatch(r"\d+\.\d+\.\d+", data["harbor_version"]) is not None, "harbor_version")
    _checksum(data["game_sha256"], "game_sha256")
    _require(data["clock_policy"] == "active-simulation", "clock_policy")
    ids = data["eligible_ids"]
    _require(isinstance(ids, list) and all(isinstance(i, str) and i.strip() == i and i for i in ids),
             "eligible_ids")
    _require(len(set(ids)) == len(ids), "eligible_ids")
    survival = None
    if campaign:
        _require(task is TaskKind.CAMPAIGN and bool(ids), "single campaign task")
        return CampaignContract(**{**data, "task": task, "eligible_ids": tuple(ids)})
    _require(task is not TaskKind.CAMPAIGN, "campaign contract revision")
    if task is TaskKind.ACHIEVEMENTS:
        _require(bool(ids), "eligible_ids")
        _require(data["survival"] is None, "survival")
    else:
        _require(not ids, "eligible_ids")
        start = data["survival"]
        _require(isinstance(start, dict), "survival")
        _exact_fields(start, set(SurvivalContract.__dataclass_fields__))
        _nonempty(start["map_id"], "survival.map_id")
        _checksum(start["map_sha256"], "survival.map_sha256")
        _require(start["seed"] is None or type(start["seed"]) is int, "survival.seed")
        _require(type(start["team_id"]) is int and 0 <= start["team_id"] <= 255, "survival.team_id")
        _nonempty(start["start_preset"], "survival.start_preset")
        _require(start["defeat"] == "last-team-core-lost", "survival.defeat")
        _require(start["allow_sector_exit"] is False, "survival.allow_sector_exit")
        survival = SurvivalContract(**start)
    return TaskContract(**{**data, "task": task, "eligible_ids": tuple(ids), "survival": survival})
