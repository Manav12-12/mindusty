FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY src /opt/verifier/src
COPY task-contract.json /opt/verifier/task-contract.json
COPY test.sh /tests/test.sh
RUN chmod 0555 /tests/test.sh
ENV PYTHONPATH=/opt/verifier/src
WORKDIR /tests
