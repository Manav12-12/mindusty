FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS desktop

# Frozen Debian snapshots make native packages reproducible, not floating apt state.
RUN rm /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260201T000000Z bookworm main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20260201T000000Z bookworm-security main\n' > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      openjdk-17-jre-headless xvfb xdotool x11-utils \
      libgl1 libgl1-mesa-dri libglx-mesa0 libxcursor1 libxrandr2 libxi6 \
      libxinerama1 libxtst6 libasound2 libgtk-3-0 fonts-dejavu-core && \
    mkdir -p /opt/benchmark && \
    dpkg-query -W > /opt/benchmark/native-packages.lock && \
    rm -rf /var/lib/apt/lists/*

COPY runtime/requirements.txt /opt/benchmark/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /opt/benchmark/requirements.txt
COPY runtime/assets/Mindustry.jar /opt/benchmark/Mindustry.jar
RUN echo '36d94941a1639478ad1d9be25a9d802c6f77552f400c6da0338b7b019f63a0d1  /opt/benchmark/Mindustry.jar' | sha256sum -c - && \
    useradd --uid 1000 --create-home player && \
    mkdir -p /state && chown 1000:1000 /state
COPY src/mindustry_benchmark /opt/benchmark/src/mindustry_benchmark
COPY runtime/launch.sh runtime/healthcheck.sh /opt/benchmark/runtime/
RUN chmod 0555 /opt/benchmark/runtime/*.sh
ENV DISPLAY=:99 HOME=/state XDG_DATA_HOME=/state/.local/share \
    XDG_CONFIG_HOME=/state/.config PYTHONPATH=/opt/benchmark/src \
    LIBGL_ALWAYS_SOFTWARE=1 ALSOFT_DRIVERS=null
USER 1000:1000
WORKDIR /state
HEALTHCHECK --interval=3s --timeout=5s --start-period=90s --retries=5 CMD ["/opt/benchmark/runtime/healthcheck.sh"]
ENTRYPOINT ["/opt/benchmark/runtime/launch.sh"]

FROM desktop AS compiler
USER root
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-17-jdk-headless && \
    rm -rf /var/lib/apt/lists/*
COPY runtime/native-service/NativeLauncher.java /build/NativeLauncher.java
RUN mkdir -p /build/classes && \
    javac --release 17 -cp /opt/benchmark/Mindustry.jar -d /build/classes /build/NativeLauncher.java

FROM desktop
USER root
COPY --from=compiler /build/classes /opt/benchmark/native-service
COPY src/mindustry_benchmark /opt/benchmark/src/mindustry_benchmark
COPY runtime/launch.sh runtime/healthcheck.sh /opt/benchmark/runtime/
RUN chmod 0555 /opt/benchmark/runtime/*.sh
ENV BENCHMARK_NATIVE_SERVICE=1
USER 1000:1000

USER root
COPY task-contract.json /opt/benchmark/task-contract.json
USER 1000:1000
