#!/usr/bin/env python3
"""
Loopback throughput benchmark: Python (pure), Python (C ext), Rust.

All three clients run inside Docker containers against a local nettest
server, so the only variable is the client implementation.

Prerequisites (started separately):
  docker run -d --name nettest-server -p 5005:5005 nettest-server
  python3 scratchpad/mock_control_server.py &

Usage:
  python3 benchmark.py            # 5 rounds each
  python3 benchmark.py -n 10      # 10 rounds each
  python3 benchmark.py --build    # force rebuild of Docker images first
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

CONTROL_PORT  = 8080
SERVER_PORT   = 5005
REPO_ROOT     = os.path.dirname(os.path.abspath(__file__))

IMAGES = {
    "rust":       {"tag": "rmbt-client-rust",   "ctx": "clientRust/"},
    "python":     {"tag": "rmbt-client-python",  "ctx": "clientPython/"},
}

CLIENTS = [
    {"label": "Python + C ext", "image": "rmbt-client-python", "env": []},
    {"label": "Python pure",    "image": "rmbt-client-python", "env": ["-e", "RMBT_PURE_PYTHON=1"]},
    {"label": "Rust",           "image": "rmbt-client-rust",   "env": []},
]

RE_DOWNLOAD = re.compile(r'Download:\s+([\d.]+)\s+Mbit/s')
RE_UPLOAD   = re.compile(r'Upload:\s+([\d.]+)\s+Mbit/s')
RE_PING_MIN = re.compile(r'Ping \(min\):\s+([\d.]+)\s+ms')


# ── Mock control server ───────────────────────────────────────────────────────

class _ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self._read_body()
        if self.path == "/RMBTControlServer/settings":
            self._json({"settings": [{"uuid": body.get("uuid") or str(uuid.uuid4())}]})
        elif self.path == "/RMBTControlServer/testRequest":
            self._json({
                "test_token":             str(uuid.uuid4()) + "_token",
                "test_uuid":              str(uuid.uuid4()),
                "open_test_uuid":         "O" + str(uuid.uuid4()),
                "test_server_address":    "127.0.0.1",
                "test_server_port":       SERVER_PORT,
                "test_server_encryption": False,
                "test_duration":          7,
                "test_numthreads":        4,
                "test_wait":              0,
                "test_server_type":       "RMBThttp",
            })
        elif self.path == "/RMBTControlServer/result":
            self._json({})
        else:
            self.send_error(404)


def _control_server_running() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=1):
            return True
    except OSError:
        return False


def start_control_server() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", CONTROL_PORT), _ControlHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── Docker helpers ────────────────────────────────────────────────────────────

def image_exists(tag: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    ).returncode == 0


def build_image(tag: str, ctx: str, force: bool = False):
    if not force and image_exists(tag):
        print(f"  {tag}: already built, skipping")
        return
    print(f"  {tag}: building from {ctx} ...")
    subprocess.run(
        ["docker", "build", "-t", tag, ctx],
        cwd=REPO_ROOT, check=True,
    )
    print(f"  {tag}: done")


def nettest_server_running() -> bool:
    r = subprocess.run(
        ["docker", "ps", "-q", "-f", "name=^nettest-server$"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


# ── Result parsing ────────────────────────────────────────────────────────────

def parse_result(output: str) -> dict | None:
    dl = RE_DOWNLOAD.search(output)
    ul = RE_UPLOAD.search(output)
    pm = RE_PING_MIN.search(output)
    if not dl or not ul:
        return None
    return {
        "download": float(dl.group(1)),
        "upload":   float(ul.group(1)),
        "ping_min": float(pm.group(1)) if pm else None,
    }


# ── Client runner ─────────────────────────────────────────────────────────────

def run_client(image: str, env_flags: list, rounds: int, label: str) -> list[dict]:
    results = []
    for i in range(rounds):
        print(f"  [{label}] round {i + 1}/{rounds} ... ", end="", flush=True)
        cmd = (
            ["docker", "run", "--rm", "--network", "host"]
            + env_flags
            + [image, "--host", f"http://127.0.0.1:{CONTROL_PORT}"]
        )
        r = subprocess.run(cmd, capture_output=True, text=True)
        res = parse_result(r.stdout)
        if res:
            results.append(res)
            print(f"DL={res['download']:8.0f}  UL={res['upload']:8.0f} Mbit/s")
        else:
            err = (r.stderr or r.stdout).strip().splitlines()
            short = err[-1] if err else "no output"
            print(f"FAILED — {short}")
    return results


# ── Statistics table ──────────────────────────────────────────────────────────

def _col(results: list[dict], key: str):
    v = [r[key] for r in results if r.get(key) is not None]
    if not v:
        return "—", "—", "—", "—"
    return (
        f"{min(v):.0f}",
        f"{max(v):.0f}",
        f"{statistics.mean(v):.0f}",
        f"{statistics.median(v):.0f}",
    )


def print_table(all_results: dict[str, list[dict]]):
    w = 14
    line = "─" * (w + 4 * 10 + 4)
    hdr  = f"  {'client':<{w}} {'min':>10} {'max':>10} {'mean':>10} {'median':>10}   Mbit/s"

    for metric, title in [("download", "DOWNLOAD"), ("upload", "UPLOAD")]:
        print(f"\n  {title}")
        print(f"  {line}")
        print(hdr)
        print(f"  {line}")
        for label, res in all_results.items():
            mn, mx, me, md = _col(res, metric)
            print(f"  {label:<{w}} {mn:>10} {mx:>10} {me:>10} {md:>10}")

    # Speedup ratios vs Python pure
    ref_dl = statistics.median([r["download"] for r in all_results.get("Python pure", [])]) if all_results.get("Python pure") else None
    ref_ul = statistics.median([r["upload"]   for r in all_results.get("Python pure", [])]) if all_results.get("Python pure") else None

    if ref_dl and ref_ul:
        print(f"\n  Speedup vs Python pure (median download / upload):")
        for label, res in all_results.items():
            if label == "Python pure" or not res:
                continue
            dl = statistics.median([r["download"] for r in res])
            ul = statistics.median([r["upload"]   for r in res])
            print(f"    {label:<{w}}  ×{dl / ref_dl:.1f} DL   ×{ul / ref_ul:.1f} UL")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Loopback benchmark: Python pure / Python+C ext / Rust",
    )
    parser.add_argument("-n", "--rounds", type=int, default=5,
                        help="Rounds per client (default: 5)")
    parser.add_argument("--build", action="store_true",
                        help="Force rebuild of Docker images")
    args = parser.parse_args()

    print("=== RMBT loopback benchmark ===\n")

    # ── 1. Build images ───────────────────────────────────────────────────────
    print("[1/3] Docker images")
    for cfg in IMAGES.values():
        build_image(cfg["tag"], cfg["ctx"], force=args.build)

    # ── 2. Infrastructure check ───────────────────────────────────────────────
    print("\n[2/3] Infrastructure")
    if not nettest_server_running():
        sys.exit("  ERROR: nettest-server container is not running.\n"
                 "  Start it with: docker run -d --name nettest-server "
                 f"-p {SERVER_PORT}:{SERVER_PORT} nettest-server")
    print(f"  nettest-server: running on port {SERVER_PORT}")

    own_control = False
    if not _control_server_running():
        srv = start_control_server()
        own_control = True
        print(f"  mock control server: started on port {CONTROL_PORT}")
    else:
        srv = None
        print(f"  mock control server: already running on port {CONTROL_PORT}")

    # ── 3. Run benchmark ──────────────────────────────────────────────────────
    print(f"\n[3/3] Benchmark ({args.rounds} rounds per client)\n")
    all_results: dict[str, list[dict]] = {}

    try:
        for client in CLIENTS:
            all_results[client["label"]] = run_client(
                client["image"], client["env"], args.rounds, client["label"]
            )
            print()
    finally:
        if own_control and srv:
            srv.shutdown()

    print_table(all_results)
    print()


if __name__ == "__main__":
    main()
