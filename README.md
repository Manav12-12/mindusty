# Mindustry Harbor environment

This checkout contains the Mindustry campaign container environment as a standard Harbor task. It does not include the custom Codex gameplay controller or any video tooling.

## Build and start

Use Linux with Docker Engine, Docker Compose, Harbor 0.24.0, and `curl`. From the repository root, fetch the pinned upstream game JAR:

```bash
assets=tasks/mindustry-campaign/environment/game/runtime/assets
mkdir -p "$assets"
curl -fL https://github.com/Anuken/Mindustry/releases/download/v160.7/Mindustry.jar \
  -o "$assets/Mindustry.jar"
echo "36d94941a1639478ad1d9be25a9d802c6f77552f400c6da0338b7b019f63a0d1  $assets/Mindustry.jar" \
  | sha256sum -c -
```

Start the task environment with Harbor:

```bash
harbor tasks start-env -p tasks/mindustry-campaign
```

To build and start just the game container without Harbor:

```bash
docker compose -f tasks/mindustry-campaign/environment/docker-compose.yaml up --build game
```

The game runs in a virtual display inside the container. These commands start the environment; they don’t launch a model or a scored trial.
