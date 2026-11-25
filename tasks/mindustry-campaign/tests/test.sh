#!/bin/sh
set -u
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt
printf '{"reward":0.0}\n' > /logs/verifier/reward.json
printf '{"status":"infrastructure_error","reason":"verifier_not_started"}\n' > /logs/verifier/diagnostics.json
python -P -m mindustry_benchmark.verifier --artifacts /state/benchmark-export --contract /opt/verifier/task-contract.json --output /logs/verifier
