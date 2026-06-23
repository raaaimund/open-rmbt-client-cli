FROM debian:bookworm-slim AS builder
RUN apt-get update && apt-get install -y \
    gcc make pkg-config \
    libcurl4-openssl-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY src/ src/
COPY Makefile .
RUN make

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y libcurl4 libssl3 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/build/rmbt-client /usr/local/bin/rmbt-client
ENTRYPOINT ["rmbt-client"]
