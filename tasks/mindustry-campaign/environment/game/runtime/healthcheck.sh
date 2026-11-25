#!/bin/sh
set -eu
# A window plus completed asset loading, not simply a live Java PID.
grep -q 'Total time to load:' /state/game.log
xdotool search --onlyvisible --name '^Mindustry' >/dev/null
python -P -m mindustry_benchmark.desktop <<'JSON' >/dev/null
{"operation":"capture"}
JSON
