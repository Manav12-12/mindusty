#!/bin/sh
set -eu
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp -noreset &
xvfb_pid=$!
game_pid=
log_pid=
cleanup() {
    if [ -n "$game_pid" ]; then kill "$game_pid" 2>/dev/null || true; fi
    if [ -n "$log_pid" ]; then kill "$log_pid" 2>/dev/null || true; fi
    kill "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM INT
tries=0
until xdpyinfo >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 50 ]; then exit 1; fi
    sleep 0.1
done
# No desktop environment, browser, terminal emulator, network listener or VNC.
if [ -z "${BENCHMARK_ATTEMPT_ID:-}" ]; then
    BENCHMARK_ATTEMPT_ID=$(python -P -c 'import uuid; print(uuid.uuid4())')
    export BENCHMARK_ATTEMPT_ID
fi
touch /state/game.log
tail -F /state/game.log &
log_pid=$!
while :; do
# Only the first, controller-observed bootstrap waits for a debugger. A native
# language continuation must not hang waiting for a lost observer; that loss is
# sticky unassessed and the successor remains playable without trusted scoring.
debug_option=
if [ "${BENCHMARK_PROFILE:-legacy}" != preventive-v2 ] &&
   [ "${BENCHMARK_NATIVE_WITNESS:-0}" = 1 ] && [ "${BENCHMARK_CONTINUATION:-0}" != 1 ]; then
    python -I -c 'import os,json; from pathlib import Path; p=Path("/state/benchmark-bootstrap.json"); p.write_text(json.dumps({"attempt_id":os.environ["BENCHMARK_ATTEMPT_ID"],"task":os.environ.get("BENCHMARK_TASK","offline-achievements")}))'
    debug_option=-agentlib:jdwp=transport=dt_socket,server=y,address=127.0.0.1:5005,suspend=y
fi
if [ "${BENCHMARK_NATIVE_SERVICE:-0}" = 1 ]; then
    java ${debug_option:+"$debug_option"} -Dnodiscord=true \
        -cp /opt/benchmark/Mindustry.jar:/opt/benchmark/native-service \
        benchmark.NativeLauncher -width 1280 -height 720 >> /state/game.log 2>&1 &
else
    java -Dnodiscord=true -jar /opt/benchmark/Mindustry.jar \
        -width 1280 -height 720 >> /state/game.log 2>&1 &
fi
game_pid=$!
status=0
wait "$game_pid" || status=$?
game_pid=
if [ "$status" -ne 73 ] || [ "${BENCHMARK_NATIVE_SERVICE:-0}" != 1 ] ||
   [ ! -f /state/benchmark-private/restart.request ] ||
   [ -f /state/benchmark-private/final.json ] || [ -f /state/benchmark-input.closed ]; then
    exit "$status"
fi
export BENCHMARK_CONTINUATION=1
done
