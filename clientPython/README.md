# rmbt-client (Python)

RMBT network measurement client written in Python. Performs ping, download, and upload phases against an RMBT measurement server and submits results to the control server.

## Requirements

- Python 3.7+
- No third-party dependencies (stdlib only: `ssl`, `socket`, `threading`, `urllib`)

## Usage

```sh
python -m rmbt_client --host https://measure.example.com
```

Run with a specific thread count and duration:

```sh
python -m rmbt_client --host https://measure.example.com --threads 4 --duration 10
```

Skip TLS verification against a local test server:

```sh
python -m rmbt_client --host https://localhost:8080 --no-tls-verify
```

### Options

| Flag | Description |
|------|-------------|
| `-h`, `--host URL` | Control server base URL **(required)** |
| `-p`, `--port PORT` | Override test server port |
| `-u`, `--uuid UUID` | Client UUID (uses/creates `~/.rmbt_client_uuid` if omitted) |
| `-t`, `--threads N` | Force thread count for download and upload (overrides pre-test) |
| `-d`, `--duration SECS` | Test duration in seconds (default: from control server) |
| `--ws` | Use WebSocket (RMBTws) framing instead of plain HTTP upgrade |
| `--http` | Use plain HTTP upgrade (RMBThttp) — overrides auto-detection |
| `--no-tls-verify` | Skip TLS certificate verification (insecure) |
| `--debug` | Print control server request/response JSON |
| `--intermediate` | Print upload throughput every 40 ms per thread |
| `--help` | Print help |

## Performance note

Python's GIL (Global Interpreter Lock) limits true parallel execution across threads. On a 100 Gbit/s back-to-back test system the client achieves roughly **8 Gbit/s downstream and 6.3 Gbit/s upstream** (with some run-to-run variation), compared to ~32 Gbit/s in both directions for the Rust client.

For typical home and office connections (up to ~1 Gbit/s) this is not a concern. On high-bandwidth links or low-powered hardware (e.g. a Raspberry Pi or a home router) the CPU may become the bottleneck before the network link is saturated, leading to results that understate the true available bandwidth.

## Protocol

1. POST `/RMBTControlServer/settings` → register client, receive UUID
2. POST `/RMBTControlServer/testRequest` → receive token, server address, thread count
3. Pre-test: 2-second single-thread GETCHUNKS download to determine chunk size and thread counts
4. Ping: 1 s / 10–100 pings
5. Download: multi-threaded GETTIME, all threads start simultaneously via `threading.Barrier`
6. Upload: multi-threaded PUTNORESULT
7. POST `/RMBTControlServer/result`

Supports both **RMBThttp** (plain HTTP upgrade) and **RMBTws** (WebSocket) variants.  
TLS via the stdlib `ssl` module; control server HTTPS via `urllib.request`.
