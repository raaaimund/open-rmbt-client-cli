import time
from dataclasses import dataclass, field
from typing import List, Tuple

SAMPLE_NS    = 40_000_000      # 40 ms in nanoseconds
READ_BLOCK   = 16 * 1024       # 16 KiB read block for download
WRITE_BLOCK  = 16 * 1024       # 16 KiB write block for upload
MAX_UL_CHUNK = 512 * 1024      # cap upload chunk at 512 KiB (see C client comment)


@dataclass
class PingResult:
    client_ns: int
    server_ns: int
    time_ns:   int


@dataclass
class TransferResult:
    bytes:      int
    elapsed_ns: int
    thread_id:  int
    samples:    List[Tuple[int, int]] = field(default_factory=list)  # (bytes, time_ns)


def _parse_time_ns(line):
    """Parse 'TIME <ns>' or any line whose last token is a decimal integer."""
    parts = line.split()
    try:
        return int(parts[-1])
    except (IndexError, ValueError):
        return 0


def run_ping(conn, duration, min_pings, max_pings):
    phase_start_ns = time.monotonic_ns()
    deadline_ns    = phase_start_ns + int(duration * 1e9)
    results        = []

    while len(results) < max_pings:
        if time.monotonic_ns() >= deadline_ns and len(results) >= min_pings:
            break

        line = conn.read_line()
        if 'PING' not in line:
            raise ConnectionError(f'Expected ACCEPT with PING, got: {line!r}')

        t0      = time.monotonic_ns()
        time_ns = t0 - phase_start_ns
        conn.write_line('PING')

        line      = conn.read_line()
        client_ns = time.monotonic_ns() - t0

        if line != 'PONG':
            raise ConnectionError(f'Expected PONG, got: {line!r}')
        conn.write_line('OK')

        line      = conn.read_line()
        server_ns = _parse_time_ns(line)

        print(f'  ping  client={client_ns / 1e6:.3f}ms  server={server_ns / 1e6:.3f}ms',
              flush=True)
        results.append(PingResult(client_ns=client_ns, server_ns=server_ns, time_ns=time_ns))

    return results


def run_download(conn, duration, chunk_size, thread_id):
    line = conn.read_line()
    if 'GETTIME' not in line:
        raise ConnectionError(f'Expected ACCEPT with GETTIME, got: {line!r}')

    conn.write_line(f'GETTIME {duration} {chunk_size}')

    rblk           = min(chunk_size, READ_BLOCK)
    t0_ns          = time.monotonic_ns()
    total          = 0
    in_chunk       = chunk_size
    last_sample_ns = t0_ns
    last_byte      = 0
    samples        = [(0, 0)]   # anchor at origin

    while True:
        want     = min(in_chunk, rblk)
        data     = conn.read_exact(want)
        total   += want
        in_chunk -= want
        last_byte = data[-1]

        now_ns = time.monotonic_ns()
        if now_ns - last_sample_ns >= SAMPLE_NS:
            samples.append((total, now_ns - t0_ns))
            last_sample_ns = now_ns

        if in_chunk == 0:
            if last_byte == 0xFF:
                break
            in_chunk = chunk_size

    conn.write_line('OK')
    line       = conn.read_line()
    elapsed_ns = _parse_time_ns(line)

    samples.append((total, elapsed_ns))

    mbps     = total * 8.0 / (elapsed_ns / 1e9) / 1e6
    client_s = (time.monotonic_ns() - t0_ns) / 1e9
    print(f'  dl[{thread_id:2d}]  {mbps:.2f} Mbit/s'
          f'  ({total} bytes in {elapsed_ns / 1e9:.3f}s, client {client_s:.3f}s)',
          flush=True)

    return TransferResult(bytes=total, elapsed_ns=elapsed_ns,
                          thread_id=thread_id, samples=samples)


def run_upload(conn, duration, chunk_size, thread_id, intermediate=False):
    line = conn.read_line()
    if 'PUT' not in line:
        raise ConnectionError(f'Expected ACCEPT with PUT/PUTNORESULT, got: {line!r}')

    conn.write_line(f'PUTNORESULT {chunk_size}')
    line = conn.read_line()
    if line != 'OK':
        raise ConnectionError(f'Expected OK after PUTNORESULT, got: {line!r}')

    # Fill chunk with i % 256 pattern (same as C client).
    pattern = bytes(range(256))
    chunk   = bytearray((pattern * (chunk_size // 256 + 1))[:chunk_size])

    wblk              = min(chunk_size, WRITE_BLOCK)
    deadline_ns       = time.monotonic_ns() + duration * 1_000_000_000
    t0_ns             = time.monotonic_ns()
    total             = 0
    last_sample_ns    = t0_ns
    last_sample_bytes = 0
    samples           = [(0, 0)]   # anchor at origin

    while True:
        terminal  = time.monotonic_ns() >= deadline_ns
        chunk[-1] = 0xFF if terminal else 0x00

        sent = 0
        while sent < chunk_size:
            want = min(chunk_size - sent, wblk)
            conn.write_bytes(bytes(chunk[sent:sent + want]))
            sent  += want
            total += want

            if not terminal:
                now_ns = time.monotonic_ns()
                if now_ns - last_sample_ns >= SAMPLE_NS:
                    samples.append((total, now_ns - t0_ns))
                    if intermediate:
                        dt = (now_ns - last_sample_ns) / 1e9
                        db = total - last_sample_bytes
                        print(f'  ul[{thread_id:2d}] +{db * 8.0 / dt / 1e6:.2f} Mbit/s',
                              flush=True)
                    last_sample_ns    = now_ns
                    last_sample_bytes = total

        if terminal:
            break

    line       = conn.read_line()
    elapsed_ns = _parse_time_ns(line)

    # Replace last sample's timestamp with the server-reported elapsed time.
    if samples:
        samples[-1] = (samples[-1][0], elapsed_ns)
    else:
        samples.append((total, elapsed_ns))

    mbps     = total * 8.0 / (elapsed_ns / 1e9) / 1e6
    client_s = (time.monotonic_ns() - t0_ns) / 1e9
    print(f'  ul[{thread_id:2d}]  {mbps:.2f} Mbit/s'
          f'  ({total} bytes in {elapsed_ns / 1e9:.3f}s, client {client_s:.3f}s)',
          flush=True)

    return TransferResult(bytes=total, elapsed_ns=elapsed_ns,
                          thread_id=thread_id, samples=samples)
