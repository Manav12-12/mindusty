"""Persistent XTest input and MSS capture; no shell or per-input subprocesses."""

import ctypes as C
import time

from .contracts import InputAction, SAFE_KEYS, _require


class X11Desktop:
    def __enter__(self):
        from mss import mss

        self._x = C.CDLL("libX11.so.6")
        self._test = C.CDLL("libXtst.so.6")
        self._x.XOpenDisplay.argtypes, self._x.XOpenDisplay.restype = [C.c_char_p], C.c_void_p
        self._x.XCloseDisplay.argtypes = [C.c_void_p]
        self._x.XSync.argtypes = [C.c_void_p, C.c_int]
        self._x.XStringToKeysym.argtypes, self._x.XStringToKeysym.restype = [C.c_char_p], C.c_ulong
        self._x.XKeysymToKeycode.argtypes = [C.c_void_p, C.c_ulong]
        self._x.XKeysymToKeycode.restype = C.c_ubyte
        for name in ("XTestFakeKeyEvent", "XTestFakeButtonEvent"):
            getattr(self._test, name).argtypes = [C.c_void_p, C.c_uint, C.c_int, C.c_ulong]
            getattr(self._test, name).restype = C.c_int
        self._test.XTestFakeMotionEvent.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_ulong]
        self._test.XTestFakeMotionEvent.restype = C.c_int
        self._display = self._x.XOpenDisplay(None)
        _require(bool(self._display), "X11 display connection")
        self._screen = None
        try:
            self._screen = mss()
            self._monitor = self._screen.monitors[1]
            self.width, self.height = self._monitor["width"], self._monitor["height"]
            self._keys = {key: self._x.XKeysymToKeycode(self._display,
                self._x.XStringToKeysym(key.encode())) for key in SAFE_KEYS}
            _require(all(self._keys.values()), "supported X11 keys")
            return self
        except BaseException:
            if self._screen is not None:
                self._screen.close()
            self._x.XCloseDisplay(self._display)
            raise

    def capture(self):
        from mss.tools import to_png

        frame = self._screen.grab(self._monitor)
        return to_png(frame.rgb, frame.size, level=1)

    def _sync(self):
        self._x.XSync(self._display, 0)

    def _key(self, key, down):
        _require(bool(self._test.XTestFakeKeyEvent(self._display, self._keys[key], down, 0)), "X11 key input")

    def _button(self, button, down):
        _require(bool(self._test.XTestFakeButtonEvent(self._display, button, down, 0)), "X11 button input")

    def _move(self, x, y):
        _require(bool(self._test.XTestFakeMotionEvent(self._display, -1, x, y, 0)), "X11 pointer input")

    def execute(self, action):
        action = InputAction.from_dict(action.to_dict(), width=self.width, height=self.height)
        args, kind = dict(action.arguments), action.kind
        if kind == "wait":
            time.sleep(args["duration"])
        elif kind == 'mouse_move':
            self._move(args['x'], args['y'])
            self._sync()
        elif kind in {'mouse_down', 'mouse_up'}:
            self._button({'left': 1, 'middle': 2, 'right': 3}[args['button']], kind == 'mouse_down')
            self._sync()
        elif kind == 'key_hold':
            pressed = []
            try:
                for key in args['keys']:
                    pressed.append(key)
                    self._key(key, True)
                self._sync()
                time.sleep(args['duration'])
            finally:
                for key in reversed(pressed):
                    self._key(key, False)
                self._sync()
        elif kind in {"key_down", "key_up", "key_press"}:
            self._key(args["key"], kind != "key_up")
            if kind == "key_press":
                self._sync()
                try:
                    # Gameplay polls held state once per frame, unlike menu events.
                    time.sleep(0.08)
                finally:
                    self._key(args["key"], False)
                    self._sync()
            self._sync()
        elif kind == "scroll":
            button = 4 if args["direction"] == "up" else 5
            for index in range(args["steps"]):
                self._button(button, True)
                self._button(button, False)
                self._sync()
                if index + 1 < args["steps"]:
                    time.sleep(0.03)
        else:
            button = {"left": 1, "middle": 2, "right": 3}[args["button"]]
            self._move(args["x"], args["y"])
            self._sync()
            # The game caches world-space hover tiles during its frame update.
            # A cursor warp and press in one frame can otherwise use the old tile.
            time.sleep(0.08)
            self._button(button, True)
            self._sync()
            try:
                if kind == 'click':
                    time.sleep(0.08)
                elif kind == "drag":
                    steps = max(1, round(args["duration"] * 30))
                    for index in range(1, steps + 1):
                        self._move(round(args["x"] + (args["to_x"] - args["x"]) * index / steps),
                            round(args["y"] + (args["to_y"] - args["y"]) * index / steps))
                        self._sync()
                        time.sleep(args["duration"] / steps)
            finally:
                self._button(button, False)
                self._sync()

    def release_inputs(self):
        for key in self._keys:
            self._key(key, False)
        for button in (1, 2, 3):
            self._button(button, False)
        self._sync()

    def __exit__(self, *errors):
        try:
            self.release_inputs()
        finally:
            try:
                self._screen.close()
            finally:
                self._x.XCloseDisplay(self._display)
