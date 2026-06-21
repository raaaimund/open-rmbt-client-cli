import time
from dataclasses import dataclass
from .connection import RmbtConn

PRETEST_DURATION = 2.0        # seconds
MIN_CHUNK        = 1024
MAX_CHUNK        = 4 * 1024 * 1024

_DL_THRESH = [100.0, 1.0, 0.0]
_DL_COUNT  = [5,     3,   1  ]
_UL_THRESH = [150.0, 5.0, 0.0]
_UL_COUNT  = [5,     3,   1  ]


def _threads_for(mbps, thresh, count):
    for t, c in zip(thresh, count):
        if mbps >= t:
            return c
    return 1


def _parse_time_ns(line):
    parts = line.split()
    try:
        return int(parts[-1])
    except (IndexError, ValueError):
        return None


@dataclass
class PretestResult:
    chunk_size: int
    dl_threads: int
    ul_threads: int


def run_pretest(addr, port, use_tls, no_tls_verify, protocol, token, max_threads):
    print('\nPre-test: measuring baseline throughput...')

    conn = RmbtConn.connect(addr, port, use_tls, no_tls_verify, protocol)
    conn.greeting(token)

    server_min = conn.chunk_size_min
    server_max = conn.chunk_size_max

    t_start    = time.monotonic()
    cs         = max(server_min, MIN_CHUNK)
    n          = 1
    last_bytes = 0
    last_ns    = 0
    rtt_ns     = 0

    try:
        while True:
            if time.monotonic() - t_start >= PRETEST_DURATION:
                break

            line = conn.read_line()
            if 'GETCHUNKS' not in line:
                raise ConnectionError(f'pre-test: expected GETCHUNKS, got: {line!r}')

            conn.write_line(f'GETCHUNKS {n} {cs}')
            for _ in range(n):
                conn.read_exact(cs)
            last_bytes = n * cs

            conn.write_line('OK')
            line = conn.read_line()
            t = _parse_time_ns(line)
            if t is not None:
                if rtt_ns == 0:
                    rtt_ns = t      # first tiny batch ≈ round-trip time
                last_ns = t

            # Exponential progression: double n up to 8, then double chunk size.
            if n >= 8:
                cs = min(cs * 2, min(server_max, MAX_CHUNK))
                n  = 1
            else:
                n *= 2
    except ConnectionError:
        pass   # server stopped after the pretest window — normal

    # TIME = transmission_time + RTT. Subtract RTT estimate to get actual throughput.
    transfer_ns = (last_ns - rtt_ns) if last_ns > rtt_ns else 0
    if transfer_ns > 0:
        bps = last_bytes / (transfer_ns / 1e9)
    elif last_ns > 0:
        bps = last_bytes / (last_ns / 1e9)
    else:
        elapsed = time.monotonic() - t_start
        bps = last_bytes / elapsed if elapsed > 0 else 0

    mbps = bps * 8.0 / 1e6

    # Target 50 chunks/sec → round to nearest KiB, clamp to server limits.
    ideal      = int(bps / 50.0)
    rounded    = ((ideal + 512) // 1024) * 1024
    cap        = min(server_max, MAX_CHUNK)
    floor_v    = max(server_min, MIN_CHUNK)
    chunk_size = max(floor_v, min(rounded or floor_v, cap))

    dl = min(_threads_for(mbps, _DL_THRESH, _DL_COUNT), max_threads)
    ul = min(_threads_for(mbps, _UL_THRESH, _UL_COUNT), max_threads)

    print(f'  pre-test: {mbps:.1f} Mbit/s → chunk={chunk_size // 1024} KiB'
          f'  dl_threads={dl}  ul_threads={ul}')

    conn.quit()
    conn.close()

    return PretestResult(chunk_size=chunk_size, dl_threads=dl, ul_threads=ul)
