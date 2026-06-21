import base64
import os
import ssl
import socket
import struct

PROTO_HTTP = 0
PROTO_WS   = 1


class RmbtConn:
    def __init__(self):
        self._sock          = None
        self._buf           = bytearray()
        self.protocol       = PROTO_HTTP
        self.chunk_size     = 4096
        self.chunk_size_min = 1024
        self.chunk_size_max = 4 * 1024 * 1024

    @classmethod
    def connect(cls, host, port, use_tls, no_tls_verify, protocol):
        c = cls()
        c.protocol = protocol
        raw = socket.create_connection((host, port), timeout=30)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if use_tls:
            ctx = ssl.create_default_context()
            if no_tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            c._sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            c._sock = raw
        if protocol == PROTO_WS:
            c._ws_upgrade(host)
        else:
            c._http_upgrade(host)
        return c

    # ── Low-level socket helpers ─────────────────────────────────────────────────

    def _sock_recv_exact(self, n):
        data = bytearray()
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError('Connection closed')
            data.extend(chunk)
        return bytes(data)

    def _read_http_headers(self):
        buf = bytearray()
        while b'\r\n\r\n' not in buf:
            b = self._sock.recv(1)
            if not b:
                raise ConnectionError('Connection closed during HTTP handshake')
            buf.extend(b)
        return buf.decode('ascii', errors='replace')

    # ── HTTP upgrade ─────────────────────────────────────────────────────────────

    def _http_upgrade(self, host):
        req = (
            f'GET /rmbt HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            f'Connection: Upgrade\r\n'
            f'Upgrade: RMBT\r\n'
            f'RMBT-Version: 1.3.5\r\n'
            f'\r\n'
        ).encode()
        self._sock.sendall(req)
        headers = self._read_http_headers()
        first = headers.split('\r\n')[0]
        if '101' not in first:
            raise ConnectionError(f'Expected HTTP 101 for RMBT upgrade, got: {first}')

    # ── WebSocket upgrade ────────────────────────────────────────────────────────

    def _ws_upgrade(self, host):
        key_b64 = base64.b64encode(os.urandom(16)).decode()
        req = (
            f'GET /rmbt HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            f'Connection: Upgrade\r\n'
            f'Upgrade: websocket\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'Sec-WebSocket-Key: {key_b64}\r\n'
            f'\r\n'
        ).encode()
        self._sock.sendall(req)
        headers = self._read_http_headers()
        first = headers.split('\r\n')[0]
        if '101' not in first:
            raise ConnectionError(f'Expected HTTP 101 for WS upgrade, got: {first}')

    # ── WebSocket framing ────────────────────────────────────────────────────────

    def _ws_send_frame(self, opcode, payload):
        if isinstance(payload, str):
            payload = payload.encode()
        mask = os.urandom(4)
        plen = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if plen <= 125:
            header.append(0x80 | plen)
        elif plen <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack('>H', plen))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('>Q', plen))
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _ws_recv_one_frame(self):
        """Read one WS frame, handle ping/close inline, return payload bytes."""
        while True:
            b0 = self._sock_recv_exact(1)[0]
            b1 = self._sock_recv_exact(1)[0]
            opcode = b0 & 0x0F
            masked = (b1 >> 7) & 1
            plen   = b1 & 0x7F
            if plen == 126:
                plen = struct.unpack('>H', self._sock_recv_exact(2))[0]
            elif plen == 127:
                plen = struct.unpack('>Q', self._sock_recv_exact(8))[0]
            mask_key = self._sock_recv_exact(4) if masked else b'\x00' * 4
            payload  = bytearray(self._sock_recv_exact(plen)) if plen else bytearray()
            if masked:
                for i in range(len(payload)):
                    payload[i] ^= mask_key[i & 3]
            if opcode == 0x9:           # PING → PONG
                self._ws_send_frame(0xA, bytes(payload))
                continue
            if opcode == 0x8:           # CLOSE
                raise ConnectionError('WebSocket closed by server')
            return bytes(payload)

    # ── Unified buffer fill ──────────────────────────────────────────────────────

    def _fill_buf(self, n):
        """Ensure self._buf contains at least n bytes."""
        if self.protocol == PROTO_WS:
            while len(self._buf) < n:
                self._buf.extend(self._ws_recv_one_frame())
        else:
            while len(self._buf) < n:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionError('Connection closed')
                self._buf.extend(chunk)

    # ── Protocol I/O ────────────────────────────────────────────────────────────

    def read_line(self):
        while b'\n' not in self._buf:
            if self.protocol == PROTO_WS:
                self._buf.extend(self._ws_recv_one_frame())
            else:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ConnectionError('Connection closed')
                self._buf.extend(chunk)
        idx  = self._buf.index(b'\n')
        line = bytes(self._buf[:idx])
        del self._buf[:idx + 1]
        # Strip \r and leading null bytes that some server implementations prepend.
        return line.rstrip(b'\r').decode('ascii', errors='replace').lstrip('\x00')

    def write_line(self, line):
        data = (line + '\n').encode()
        if self.protocol == PROTO_WS:
            self._ws_send_frame(0x1, data)   # text frame
        else:
            self._sock.sendall(data)

    def read_exact(self, n):
        self._fill_buf(n)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def write_bytes(self, data):
        if self.protocol == PROTO_WS:
            self._ws_send_frame(0x2, data)   # binary frame
        else:
            self._sock.sendall(data)

    # ── RMBT handshake ──────────────────────────────────────────────────────────

    def greeting(self, token):
        line = self.read_line()
        if not line.startswith('RMBTv'):
            raise ConnectionError(f'Unexpected greeting: {line!r}')
        line = self.read_line()
        if 'TOKEN' not in line:
            raise ConnectionError(f'Server did not offer TOKEN: {line!r}')
        self.write_line(f'TOKEN {token}')
        line = self.read_line()
        if line != 'OK':
            raise ConnectionError(f'Token rejected: {line!r}')
        line = self.read_line()
        if line.startswith('CHUNKSIZE'):
            parts = line.split()
            if len(parts) >= 2: self.chunk_size     = int(parts[1])
            if len(parts) >= 3: self.chunk_size_min = int(parts[2])
            if len(parts) >= 4: self.chunk_size_max = int(parts[3])

    def quit(self):
        try:
            self.read_line()        # discard pending ACCEPT line
            self.write_line('QUIT')
            self.read_line()        # discard BYE
        except Exception:
            pass

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass
