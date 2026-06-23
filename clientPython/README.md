# rmbt-client (Python)

[![PyPI](https://img.shields.io/pypi/v/rmbt-client)](https://pypi.org/project/rmbt-client/)

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

## Performance

Starting with v1.1.0 the download and upload hot loops run inside a small C extension (`rmbt_loop`) that calls `Py_BEGIN_ALLOW_THREADS` before entering the tight `recv()`/`send()` loop. This releases the GIL for the entire bulk-data phase, allowing all measurement threads to transfer data truly in parallel.

The extension is compiled automatically when the package is installed from source (requires `gcc` and `python3-dev`). Pre-built **manylinux wheels** are published to PyPI for the three architectures Home Assistant runs on:

| Architecture | Covers |
|---|---|
| `x86_64` | NUC, generic x86 VM, Docker on x86 |
| `aarch64` | Raspberry Pi 4 / 5, modern ARM boards (64-bit) |
| `armv7l` | Raspberry Pi 3 and older (32-bit) |

When the extension is not available (e.g. unsupported platform, missing compiler) the client falls back silently to the pure-Python implementation.

### Measured throughput

The numbers below are from a **loopback test** (`127.0.0.1`, 4 threads, 7 s). They reflect CPU throughput, not a real network:

**Test system:** Intel Core i7-1165G7 @ 2.80 GHz (4 cores / 8 threads, 11th Gen), 24 GB RAM, x86_64 Linux

| Client | Download | Upload |
|---|---|---|
| Python 1.1.0 (C extension, loopback) | ~94 Gbit/s | ~68 Gbit/s |
| Python 1.0.0 (pure Python, loopback) | ~8 Gbit/s | ~6 Gbit/s |
| Rust (loopback) | ~32 Gbit/s | ~32 Gbit/s |

> **No test on a real 100 Gbit/s network has been performed.** On an actual high-speed link, factors such as NIC driver overhead, interrupt coalescing, and kernel socket buffer tuning will dominate long before the Python vs. C difference matters. For typical home and office connections (up to ~10 Gbit/s) either implementation is more than fast enough.

To force the pure-Python path for benchmarking or debugging:

```sh
RMBT_PURE_PYTHON=1 python -m rmbt_client --host https://measure.example.com
```

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
