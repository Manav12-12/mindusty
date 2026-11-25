"""Trusted native finish/export transport, never exposed as a world-query tool."""

import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

from .contracts import ClockSample, ContractError, NativeState, CampaignState, _exact_fields, _require, _unique_object
from .contracts import native_survival_start


GAME_SHA256 = "36d94941a1639478ad1d9be25a9d802c6f77552f400c6da0338b7b019f63a0d1"
END_REASONS = frozenset({"agent_finish", "natural_defeat", "stagnation", "policy_violation",
                          "infrastructure_error", "external_stop", "objective_complete", "subscription_limit"})


def decode_survival_start(raw: bytes, start, attempt_id: str) -> dict:
    """Verify the actual native starting world, not an instruction's assertion."""
    native_survival_start(start)
    data = json.loads(raw, object_pairs_hook=_unique_object)
    expected = {"attempt_id": attempt_id, "map_id": start.map_id,
        "map_sha256": start.map_sha256, "seed": None, "preset": start.start_preset,
        "team_id": start.team_id, "core_count": 1, "width": 256, "height": 256,
        "waves": True, "attack_mode": False, "win_wave": 0, "starting_copper": 200}
    _exact_fields(data, set(expected))
    for field, value in expected.items():
        _require(type(data[field]) is type(value) and data[field] == value,
                 f"native survival start {field}")
    return data


def _valid_seconds(seconds: object) -> bool:
    try:
        return type(seconds) in (int, float) and seconds >= 0 and math.isfinite(seconds)
    except OverflowError:
        return False


def decode_reply(raw: bytes, expected_challenge: str) -> dict:
    """Reject static/stale files; not authentication against in-process code execution."""
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError) as error:
        raise ContractError("Unreadable native reply") from error
    _require(isinstance(data, dict) and data.get("challenge") == expected_challenge,
             "fresh reply challenge")
    return {key: value for key, value in data.items() if key != "challenge"}


def decode_clock(raw: bytes, expected_attempt_id: str) -> ClockSample:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError) as error:
        raise ContractError("Unreadable clock JSON") from error
    _exact_fields(data, set(ClockSample.__dataclass_fields__))
    _require(data["attempt_id"] == expected_attempt_id, "attempt_id")
    _require(_valid_seconds(data["play_seconds"]), "play_seconds")
    _require(type(data["finalized"]) is bool, "finalized")
    _require(type(data["has_played"]) is bool, "has_played")
    _require(type(data["protected_work"]) is bool, "protected_work")
    return ClockSample(**data)


def decode_final(raw: bytes, expected_attempt_id: str) -> NativeState:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError) as error:
        raise ContractError("Unreadable final native JSON") from error
    _require(isinstance(data, dict) and type(data.get("format_version")) is int and
             data["format_version"] in {1, 2}, "format_version")
    campaign = data["format_version"] == 2
    _exact_fields(data, set(CampaignState.__dataclass_fields__ if campaign else NativeState.__dataclass_fields__))
    _require(data["attempt_id"] == expected_attempt_id, "attempt_id")
    _require(data["game_version"] == "v160.7", "game_version")
    _require(data["game_sha256"] == GAME_SHA256, "game_sha256")
    _require(data["finalized"] is True, "finalized")
    _require(_valid_seconds(data["play_seconds"]), "play_seconds")
    for field in ("known_ids", "achieved_ids"):
        ids = data[field]
        _require(isinstance(ids, list) and all(isinstance(i, str) and i for i in ids), field)
        _require(len(set(ids)) == len(ids), field)
    _require(bool(data["known_ids"]), "known_ids")
    _require(set(data["achieved_ids"]) <= set(data["known_ids"]), "achieved_ids")
    _require(isinstance(data["reason"], str) and data["reason"] in END_REASONS, "reason")
    _require(type(data["team_id"]) is int and 0 <= data["team_id"] <= 255, "team_id")
    _require(type(data["cores_alive"]) is int and data["cores_alive"] >= 0, "cores_alive")
    _require(type(data["game_over"]) is bool, "game_over")
    _require(type(data["has_played"]) is bool, "has_played")
    if campaign:
        times = data["achievement_times"]
        _require(isinstance(times, dict) and set(times) == set(data["achieved_ids"]), "native milestone coverage")
        _require(all(_valid_seconds(seconds) and seconds <= data["play_seconds"]
                     for seconds in times.values()), "native milestone time")
    state_type = CampaignState if campaign else NativeState
    return state_type(**{**data, "known_ids": tuple(data["known_ids"]),
                          "achieved_ids": tuple(data["achieved_ids"])})


def decode_progress(raw: bytes, expected_attempt_id: str) -> dict:
    data = json.loads(raw, object_pairs_hook=_unique_object)
    _require(isinstance(data, dict), "native progress")
    has_times = "achievement_times" in data
    _exact_fields(data, set(ClockSample.__dataclass_fields__) | {"achieved_ids"} |
        ({"achievement_times"} if has_times else set()))
    decode_clock(json.dumps({k: data[k] for k in ClockSample.__dataclass_fields__}).encode(), expected_attempt_id)
    ids = data["achieved_ids"]
    _require(isinstance(ids, list) and all(isinstance(i, str) and i for i in ids) and
        len(ids) == len(set(ids)), "progress achievement IDs")
    if has_times:
        times = data['achievement_times']
        _require(isinstance(times, dict) and set(times) == set(ids) and
            all(_valid_seconds(value) and value <= data['play_seconds'] for value in times.values()),
            "progress milestone times")
    return data


class NativeChannel:
    def __init__(self, container_id: str, attempt_id: str):
        _require(re.fullmatch(r"[a-f0-9]{12,64}", container_id) is not None, "container_id")
        self._container = container_id
        self._attempt = attempt_id
        self._final_bytes = None

    def _request(self, operation: str, **fields) -> bytes:
        challenge = secrets.token_hex(16)
        raw = subprocess.run([
            "docker", "exec", "-i", self._container, "python", "-P", "-m",
            "mindustry_benchmark.native_state",
        ], input=json.dumps({"operation": operation, "attempt_id": self._attempt,
                             "challenge": challenge, **fields}).encode(),
            capture_output=True, check=True, timeout=40).stdout
        return json.dumps(decode_reply(raw, challenge), separators=(",", ":")).encode()

    def finish(self, reason: str) -> NativeState:
        _require(reason in END_REASONS, "reason")
        raw = self._request("finish", reason=reason)
        state = decode_final(raw, self._attempt)
        self._final_bytes = raw
        return state

    def read_final_bytes(self) -> bytes:
        _require(self._final_bytes is not None, "verified finalization required")
        return self._final_bytes

    def read_clock(self) -> ClockSample:
        """Trusted lifecycle/watchdog clock, not an agent observation endpoint."""
        return decode_clock(self._request("read_clock"), self._attempt)

    def read_progress(self) -> dict:
        return decode_progress(self._request("read_progress"), self._attempt)

    def read_policy_bytes(self) -> bytes:
        """Trusted audit collection only; no game state or raw exec tool is exposed."""
        script = ("from pathlib import Path; import sys; "
                  "p=Path('/state/benchmark-private/policy-events.jsonl'); "
                  "sys.stdout.buffer.write(p.read_bytes() if p.exists() else b'')")
        return subprocess.run(["docker", "exec", self._container, "python", "-P", "-c", script],
                              capture_output=True, check=True, timeout=15).stdout

    def read_survival_start(self) -> bytes:
        return subprocess.run(["docker", "exec", self._container, "cat",
            "/state/benchmark-private/survival-start.json"],
            capture_output=True, check=True, timeout=15).stdout


class PipeNativeChannel(NativeChannel):
    def __init__(self, container_id, attempt_id, pipe):
        super().__init__(container_id, attempt_id)
        self._pipe = pipe

    def _request(self, operation, **fields):
        challenge = secrets.token_hex(16)
        raw = self._pipe.request("native_state", {"operation": operation, "attempt_id": self._attempt,
            "challenge": challenge, **fields})
        return json.dumps(decode_reply(raw, challenge), separators=(",", ":")).encode()


def request_bytes(payload) -> bytes:
    """Fixed native protocol, shared by one-shot CLI and persistent private pipe."""
    _require(isinstance(payload, dict), "native request")
    expected_attempt = os.environ.get("BENCHMARK_ATTEMPT_ID")
    if expected_attempt is None:
        identity = json.loads(Path("/state/benchmark-private/identity.json").read_bytes(),
                              object_pairs_hook=_unique_object)
        expected_attempt = identity["attempt_id"]
    _require(payload.get("attempt_id") == expected_attempt and
             bool(payload.get("attempt_id")), "attempt_id")
    final = Path("/state/benchmark-private/final.json")
    operation = payload.get("operation")
    challenge = payload.get("challenge")
    _require(isinstance(challenge, str) and re.fullmatch(r"[0-9a-f]{32}", challenge) is not None,
             "challenge")
    if operation in {"read_clock", "read_progress"}:
        _exact_fields(payload, {"operation", "attempt_id", "challenge"})
        request = Path("/state/benchmark-private/clock.request")
        reply = Path("/state/benchmark-private/clock.json")
        decoder = decode_progress if operation == "read_progress" else decode_clock
    elif operation == "finish":
        _exact_fields(payload, {"operation", "attempt_id", "challenge", "reason"})
        _require(payload["reason"] in END_REASONS, "reason")
        request = Path("/state/benchmark-private/finish.request")
        reply = final
        decoder = decode_final
    else:
        raise ContractError("Unknown native operation")
    # Require a fresh game-thread acknowledgement even when a file already exists.
    with tempfile.NamedTemporaryFile(mode="w", dir=request.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream)
    try:
        os.replace(temporary, request)
    finally:
        temporary.unlink(missing_ok=True)
    # A declared same-attempt language continuation must initialize the real
    # renderer again. This is a bounded infrastructure wait, not extra play time,
    # an agent retry, or permission to wait forever on an ordinary outage.
    restarting = Path("/state/benchmark-private/restart.in-progress").exists()
    deadline = time.monotonic() + (35 if restarting else 15)
    while time.monotonic() < deadline:
        try:
            raw = reply.read_bytes()
            body = decode_reply(raw, challenge)
        except (OSError, ContractError):
            time.sleep(0.05)
            continue
        decoder(json.dumps(body).encode(), payload["attempt_id"])
        return raw
    raise TimeoutError("Native request did not produce a fresh acknowledgement")


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read(4097), object_pairs_hook=_unique_object)
    sys.stdout.buffer.write(request_bytes(payload))


if __name__ == "__main__":
    main()
