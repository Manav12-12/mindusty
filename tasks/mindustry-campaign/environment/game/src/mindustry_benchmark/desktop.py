"""Fixed-operation X11 backend and trusted Docker transport. No model-facing shell."""

import json
import fcntl
from pathlib import Path
import re
import subprocess
import sys
import time
import os
import select
import threading

from .contracts import ContractError, InputAction, SAFE_KEYS, _require, _exact_fields, _unique_object


class GamePipe:
    """One owned fixed-operation subprocess, binary responses, no reconnect/replay."""

    def __init__(self, command):
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        self._buffer = bytearray()
        self._sequence = 0
        self._lock = threading.Lock()
        self.closed = False

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([self._process.stdout], [], [], remaining)[0]:
            raise TimeoutError("Game pipe response timeout")
        raw = os.read(self._process.stdout.fileno(), 65536)
        _require(bool(raw), "game pipe disconnected")
        self._buffer.extend(raw)

    def request(self, module, payload, *, timeout=45):
        with self._lock:
            _require(not self.closed, "game pipe closed")
            _require(module in {"desktop", "native_state"} and isinstance(payload, dict), "fixed game module")
            _require(type(timeout) in (int, float) and 0 < timeout <= 120, "game pipe timeout")
            self._sequence += 1
            raw = json.dumps({"id": self._sequence, "module": module, "payload": payload},
                allow_nan=False, separators=(",", ":")).encode() + b"\n"
            _require(len(raw) <= 16384, "bounded game request")
            deadline = time.monotonic() + timeout
            try:
                remaining = memoryview(raw)
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not select.select([], [self._process.stdin], [], wait)[1]:
                        raise TimeoutError("Game pipe write timeout")
                    try:
                        count = os.write(self._process.stdin.fileno(), remaining)
                    except BlockingIOError:
                        continue
                    _require(count > 0, "game pipe write")
                    remaining = remaining[count:]
                while b"\n" not in self._buffer:
                    _require(len(self._buffer) <= 4096, "bounded game response header")
                    self._receive(deadline)
                line, _, body = self._buffer.partition(b"\n")
                _require(len(line) <= 4096, "bounded game response header")
                header = json.loads(line, object_pairs_hook=_unique_object)
                _exact_fields(header, {"id", "ok", "length"})
                _require(type(header["id"]) is int and header["id"] == self._sequence and
                    header["ok"] is True and type(header["length"]) is int and
                    0 <= header["length"] <= 16 * 1024 * 1024, "game pipe response")
                self._buffer = bytearray(body)
                length = header["length"]
                while len(self._buffer) < length:
                    self._receive(deadline)
                result = bytes(self._buffer[:length])
                del self._buffer[:length]
                return result
            except BaseException:
                self.close()
                raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._process.stdin.close()  # EOF makes the owned server release held input.
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        finally:
            self._process.stdout.close()


def serve_pipe(source, target, screen, *, state_dir=Path("/state")):
    """Controller-only stdio service. Invalid requests never expose exception text."""
    sequence = 0
    try:
        while True:
            line = source.readline(16385)
            if not line:
                break
            identity = None
            try:
                _require(len(line) <= 16384 and line.endswith(b"\n"), "bounded game request")
                request = json.loads(line, object_pairs_hook=_unique_object)
                _exact_fields(request, {"id", "module", "payload"})
                identity = request["id"]
                _require(type(identity) is int and identity == sequence + 1, "game request sequence")
                sequence = identity
                module, payload = request["module"], request["payload"]
                _require(isinstance(payload, dict), "game payload")
                operation = payload.get("operation")
                if module == "desktop":
                    if operation == "capture":
                        _exact_fields(payload, {"operation"})
                        raw = screen.capture()
                    elif operation == "release":
                        _exact_fields(payload, {"operation"})
                        screen.release_inputs()
                        raw = b""
                    else:
                        _require(operation in {"input", "input_capture"}, "fixed desktop operation")
                        _exact_fields(payload, {"operation", "action"})
                        action = InputAction.from_dict(payload["action"], width=screen.width, height=screen.height)
                        with (state_dir / "benchmark-input.lock").open("a") as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX)
                            _require(not (state_dir / "benchmark-input.closed").exists(), "input closed")
                            screen.execute(action)
                        raw = screen.capture() if operation == "input_capture" else b""
                elif module == "native_state":
                    if operation == "read_policy":
                        _exact_fields(payload, {"operation"})
                        path = state_dir / "benchmark-private/policy-events.jsonl"
                        raw = path.read_bytes() if path.exists() else b""
                    else:
                        from .native_state import request_bytes
                        raw = request_bytes(payload)
                else:
                    raise ContractError("fixed game module")
                _require(isinstance(raw, bytes) and len(raw) <= 16 * 1024 * 1024, "bounded game reply")
                header = {"id": identity, "ok": True, "length": len(raw)}
                target.write(json.dumps(header, separators=(",", ":")).encode() + b"\n" + raw)
                target.flush()
            except (OSError, ValueError, TypeError):
                target.write(json.dumps({"id": identity, "ok": False, "length": 0}).encode() + b"\n")
                target.flush()
                break  # No resynchronization, automatic replay or silent fallback.
    finally:
        if screen is not None:
            screen.release_inputs()


class DockerDesktop:
    def __init__(self, container_id: str):
        if re.fullmatch(r"[a-f0-9]{12,64}", container_id) is None:
            raise ContractError("Invalid trusted container ID")
        self.container_id = container_id

    def _request(self, payload: dict) -> bytes:
        return subprocess.run([
            "docker", "exec", "-i", self.container_id,
            "python", "-P", "-m", "mindustry_benchmark.desktop",
        ], input=json.dumps(payload).encode(), capture_output=True,
            check=True, timeout=45).stdout

    def capture(self) -> bytes:
        return self._request({"operation": "capture"})

    def execute(self, action: InputAction) -> None:
        self._request({"operation": "input", "action": action.to_dict()})

    def release_inputs(self) -> None:
        self._request({"operation": "release"})


class PipeDesktop:
    def __init__(self, pipe):
        self._pipe = pipe

    def capture(self):
        return self._pipe.request("desktop", {"operation": "capture"})

    def execute_capture(self, action):
        return self._pipe.request("desktop", {"operation": "input_capture", "action": action.to_dict()})

    def execute(self, action):
        self._pipe.request('desktop', {'operation': 'input', 'action': action.to_dict()})

    def release_inputs(self):
        return self._pipe.request("desktop", {"operation": "release"})


def _xdotool(*args: str) -> None:
    subprocess.run(["xdotool", *args], check=True, capture_output=True, timeout=5)


def _release() -> None:
    # Release all supported keys, including those held across separate tool requests.
    _xdotool("keyup", *sorted(SAFE_KEYS))
    for button in ("1", "2", "3"):
        _xdotool("mouseup", button)


def _input(action: InputAction) -> None:
    args = dict(action.arguments)
    kind = action.kind
    if kind == "wait":
        time.sleep(args["duration"])
    elif kind == 'mouse_move':
        _xdotool('mousemove', str(args['x']), str(args['y']), 'getmouselocation')
    elif kind in {'mouse_down', 'mouse_up'}:
        button = {'left': '1', 'middle': '2', 'right': '3'}[args['button']]
        _xdotool('mousedown' if kind == 'mouse_down' else 'mouseup', button)
    elif kind == 'key_hold':
        pressed = []
        try:
            for key in args['keys']:
                pressed.append(key)
                _xdotool('keydown', key)
            time.sleep(args['duration'])
        finally:
            for key in reversed(pressed):
                _xdotool('keyup', key)
    elif kind in ("key_down", "key_up"):
        _xdotool("keydown" if kind == "key_down" else "keyup", args["key"])
    elif kind == "key_press":
        _xdotool('keydown', args['key'])
        try:
            time.sleep(0.08)
        finally:
            _xdotool('keyup', args['key'])
    elif kind == "scroll":
        _xdotool("click", "--repeat", str(args["steps"]), "--delay", "30",
                 "4" if args["direction"] == "up" else "5")
    else:
        button = {"left": "1", "middle": "2", "right": "3"}[args["button"]]
        # --sync waits for motion and hangs when already at the target. A pointer
        # query provides a synchronous X-server round trip even with zero motion.
        _xdotool("mousemove", str(args["x"]), str(args["y"]), "getmouselocation")
        time.sleep(0.08)
        if kind == "click":
            _xdotool('mousedown', button)
            try:
                time.sleep(0.08)
            finally:
                _xdotool('mouseup', button)
        else:
            try:
                _xdotool("mousedown", button)
                # Interpolate mouse motion, not strategic gameplay macros.
                steps = max(1, round(args["duration"] * 30))
                for index in range(1, steps + 1):
                    x = round(args["x"] + (args["to_x"] - args["x"]) * index / steps)
                    y = round(args["y"] + (args["to_y"] - args["y"]) * index / steps)
                    _xdotool("mousemove", str(x), str(y), "getmouselocation")
                    time.sleep(args["duration"] / steps)
            finally:
                _xdotool("mouseup", button)


def close_inputs() -> None:
    """Trusted collection boundary; finish an in-flight bounded action, then close."""
    with Path("/state/benchmark-input.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Path("/state/benchmark-input.closed").touch(exist_ok=True)
        _release()


def _guarded_input(action: InputAction) -> None:
    with Path("/state/benchmark-input.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if Path("/state/benchmark-input.closed").exists():
            raise ContractError("attempt input permanently closed")
        _input(action)


def main() -> None:
    # Only this fixed protocol is reachable through the trusted host transport.
    from mss import mss
    from mss.tools import to_png

    if sys.argv[1:] == ["--pipe"]:
        from .x11 import X11Desktop
        with X11Desktop() as screen:
            serve_pipe(sys.stdin.buffer, sys.stdout.buffer, screen)
        return
    payload = json.loads(sys.stdin.buffer.read(16385))
    if not isinstance(payload, dict):
        raise ContractError("Invalid desktop request")
    operation = payload.get("operation")
    if operation == "release" and set(payload) == {"operation"}:
        _release()
        return
    with mss() as screen:
        monitor = screen.monitors[1]
        if operation == "capture" and set(payload) == {"operation"}:
            frame = screen.grab(monitor)
            sys.stdout.buffer.write(to_png(frame.rgb, frame.size))
        elif operation == "input" and set(payload) == {"operation", "action"}:
            action = InputAction.from_dict(payload["action"], width=monitor["width"],
                                           height=monitor["height"])
            _guarded_input(action)
        else:
            raise ContractError("Unknown desktop operation")


if __name__ == "__main__":
    main()
