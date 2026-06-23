import argparse
import sys
import time
import threading
from urllib.parse import urlparse

from . import control, connection, pretest, tests, uuid_store

VERSION     = '1.0.0'
MAX_THREADS = 20


def _run_phase(n, addr, port, use_tls, no_tls_verify, protocol,
               token, duration, chunk_size, intermediate, phase):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        try:
            conn = connection.RmbtConn.connect(addr, port, use_tls, no_tls_verify, protocol)
            conn.greeting(token)
        except Exception as e:
            barrier.wait()
            print(f'[thread {i}] connect failed (skipping): {e}', file=sys.stderr)
            return
        barrier.wait()
        try:
            if phase == 'download':
                results[i] = tests.run_download(conn, duration, chunk_size, i)
            else:
                results[i] = tests.run_upload(conn, duration, chunk_size, i, intermediate)
            conn.quit()
        except Exception as e:
            print(f'[thread {i}] dropped (skipping): {e}', file=sys.stderr)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return [r for r in results if r is not None]


def main():
    parser = argparse.ArgumentParser(
        prog='rmbt-nettest',
        description='RMBT network measurement client',
        add_help=False,
    )
    parser.add_argument('--help', action='help')
    parser.add_argument('-h', '--host',         required=True, metavar='URL',
                        help='Control server base URL (required)')
    parser.add_argument('-p', '--port',         type=int,  metavar='PORT',
                        help='Override test server port')
    parser.add_argument('-u', '--uuid',         metavar='UUID',
                        help='Client UUID (uses/creates ~/.rmbt_nettest_uuid if omitted)')
    parser.add_argument('-t', '--threads',      type=int,  metavar='N',
                        help='Force thread count for download and upload')
    parser.add_argument('-d', '--duration',     type=int,  metavar='SECS',
                        help='Test duration in seconds (default: from control server)')
    parser.add_argument('--ws',          action='store_true',
                        help='Use WebSocket (RMBTws) framing')
    parser.add_argument('--http',        action='store_true',
                        help='Use plain HTTP upgrade (RMBThttp, default)')
    parser.add_argument('--no-tls-verify', action='store_true',
                        help='Skip TLS certificate verification (insecure)')
    parser.add_argument('--debug',       action='store_true',
                        help='Print control server request/response JSON')
    parser.add_argument('--intermediate', action='store_true',
                        help='Print upload throughput every 40 ms per thread')
    args = parser.parse_args()

    if args.ws and args.http:
        parser.error('--ws and --http are mutually exclusive')

    host = args.host
    if not host.startswith('http://') and not host.startswith('https://'):
        host = 'https://' + host

    # ── UUID resolution ──────────────────────────────────────────────────────────
    if args.uuid:
        uuid = args.uuid
    else:
        stored = uuid_store.load()
        uuid   = control.request_settings(host, stored, VERSION, args.debug)
        if uuid != stored:
            uuid_store.save(uuid)

    # ── Step 1: request test parameters ─────────────────────────────────────────
    print(f'Contacting control server: {host}')
    params = control.request_test(host, uuid, VERSION, args.ws, args.debug)

    print(f'Token:    {params.token[:40]}...')
    print(f'Server:   {params.server_addr}:{params.server_port}'
          f' ({"TLS" if params.encryption else "plain TCP"})')

    if args.ws:
        protocol = connection.PROTO_WS
    elif args.http or params.server_type != 'RMBTws':
        protocol = connection.PROTO_HTTP
    else:
        protocol = connection.PROTO_WS

    proto_name  = 'RMBTws' if protocol == connection.PROTO_WS else 'RMBThttp'
    server_type = params.server_type or 'unset'
    print(f'Protocol: {proto_name}  (server_type: {server_type})')

    if params.wait > 0:
        print(f'Waiting {params.wait}s before test...')
        time.sleep(params.wait)

    port          = args.port     or params.server_port
    duration      = args.duration or params.duration
    server_cap    = min(params.num_threads, MAX_THREADS)
    no_tls_verify = args.no_tls_verify

    # ── Step 2: pre-test ─────────────────────────────────────────────────────────
    pt = pretest.run_pretest(
        params.server_addr, port, params.encryption, no_tls_verify,
        protocol, params.token, server_cap,
    )

    dl_threads = max(1, min(args.threads or pt.dl_threads, server_cap))
    ul_threads = max(1, min(args.threads or pt.ul_threads, server_cap))
    dl_chunk   = pt.chunk_size
    ul_chunk   = min(dl_chunk, tests.MAX_UL_CHUNK)

    print(f'\nTest plan: dl_threads={dl_threads}  ul_threads={ul_threads}'
          f'  dl_chunk={dl_chunk // 1024} KiB  ul_chunk={ul_chunk // 1024} KiB'
          f'  duration={duration}s')

    test_begin_ms = int(time.time() * 1000)

    # ── Step 3: ping ─────────────────────────────────────────────────────────────
    print('\nPing (1 s, 10-100 pings):')
    ping_conn = connection.RmbtConn.connect(
        params.server_addr, port, params.encryption, no_tls_verify, protocol)
    ping_conn.greeting(params.token)
    ping_results = tests.run_ping(ping_conn, 1.0, 10, 100)
    ping_conn.quit()
    ping_conn.close()

    # ── Step 4: download ─────────────────────────────────────────────────────────
    print(f'\nDownload ({dl_threads} thread(s), {duration}s):')
    dl_results = _run_phase(
        dl_threads, params.server_addr, port, params.encryption, no_tls_verify,
        protocol, params.token, duration, dl_chunk, False, 'download',
    )
    if not dl_results:
        sys.exit('All download threads failed')

    # ── Step 5: upload ───────────────────────────────────────────────────────────
    print(f'\nUpload ({ul_threads} thread(s), {duration}s):')
    ul_results = _run_phase(
        ul_threads, params.server_addr, port, params.encryption, no_tls_verify,
        protocol, params.token, duration, ul_chunk, args.intermediate, 'upload',
    )
    if not ul_results:
        sys.exit('All upload threads failed')

    # ── Step 6: aggregate ────────────────────────────────────────────────────────
    dl_bytes = sum(r.bytes      for r in dl_results)
    dl_ns    = max(r.elapsed_ns for r in dl_results)
    ul_bytes = sum(r.bytes      for r in ul_results)
    ul_ns    = max(r.elapsed_ns for r in ul_results)

    dl_mbps = dl_bytes * 8.0 / (dl_ns / 1e9) / 1e6
    ul_mbps = ul_bytes * 8.0 / (ul_ns / 1e9) / 1e6

    sorted_client_ns      = sorted(r.client_ns for r in ping_results)
    ping_min_ms           = sorted_client_ns[0]  / 1e6 if ping_results else 0
    ping_median_ms        = sorted_client_ns[len(sorted_client_ns) // 2] / 1e6 if ping_results else 0
    ping_shortest_server  = min(r.server_ns for r in ping_results) if ping_results else 0

    print('\n=== Results ===')
    print(f'Ping (min):     {ping_min_ms:7.2f} ms  ({len(ping_results)} pings)')
    print(f'Ping (median):  {ping_median_ms:7.2f} ms')
    print(f'Download:       {dl_mbps:7.2f} Mbit/s'
          f'  ({dl_bytes} bytes in {dl_ns / 1e9:.2f}s, {len(dl_results)} thread(s))')
    print(f'Upload:         {ul_mbps:7.2f} Mbit/s'
          f'  ({ul_bytes} bytes in {ul_ns / 1e9:.2f}s, {len(ul_results)} thread(s))')

    if params.open_test_uuid:
        _base = urlparse(host)
        _share_url = f"{_base.scheme}://{_base.netloc}/share/{params.open_test_uuid}"
        print(f'Result:         {_share_url}')

    # ── Step 7: submit results ───────────────────────────────────────────────────
    print('\nSubmitting results to control server...')

    speed_detail = []
    for r in dl_results:
        for (b, t) in r.samples:
            speed_detail.append({'direction': 'download', 'thread': r.thread_id,
                                 'time': t, 'bytes': b})
    for r in ul_results:
        for (b, t) in r.samples:
            speed_detail.append({'direction': 'upload', 'thread': r.thread_id,
                                 'time': t, 'bytes': b})

    result = {
        'client_language':         'en',
        'client_name':             'RMBTws' if protocol == connection.PROTO_WS else 'RMBT',
        'client_uuid':             uuid,
        'client_version':          VERSION,
        'client_software_version': VERSION,
        'geoLocations':            [],
        'model':                   'Client CLI Python',
        'network_type':            98,
        'platform':                'CLI',
        'product':                 'rmbt-nettest-python',
        'pings':                   [{'value': r.client_ns, 'value_server': r.server_ns,
                                     'time_ns': r.time_ns} for r in ping_results],
        'test_bytes_download':     dl_bytes,
        'test_bytes_upload':       ul_bytes,
        'test_nsec_download':      dl_ns,
        'test_nsec_upload':        ul_ns,
        'test_num_threads':        len(dl_results),
        'num_threads_ul':          len(ul_results),
        'test_ping_shortest':      ping_shortest_server,
        'test_speed_download':     int(dl_bytes * 8e6 / dl_ns),
        'test_speed_upload':       int(ul_bytes * 8e6 / ul_ns),
        'test_token':              params.token,
        'test_uuid':               params.test_uuid,
        'time':                    test_begin_ms,
        'timezone':                'UTC',
        'type':                    'DESKTOP',
        'version_code':            '1',
        'speed_detail':            speed_detail,
        'user_server_selection':   False,
        'test_status':             '0',
        'test_port_remote':        port,
    }

    control.submit_result(host, result, args.debug)


if __name__ == '__main__':
    main()
