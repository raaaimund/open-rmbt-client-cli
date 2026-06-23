FROM python:3.13-slim AS builder

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends gcc python3-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml setup.py ./
COPY rmbt_client/ rmbt_client/

RUN pip install --no-cache-dir setuptools wheel build \
 && python -m build --wheel --outdir /dist .


FROM python:3.13-slim

COPY --from=builder /dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl \
 && rm /tmp/*.whl

ENTRYPOINT ["rmbt-client"]
