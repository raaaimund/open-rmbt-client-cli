#!/usr/bin/env python3
"""Minimal mock RMBT control server for local testing."""
import json
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

TEST_SERVER_HOST = "127.0.0.1"
TEST_SERVER_PORT = 5005

CLIENT_UUID = str(uuid.uuid4())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[control] {fmt % args}")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self._read_body()

        if self.path == "/RMBTControlServer/settings":
            client_uuid = body.get("uuid") or CLIENT_UUID
            print(f"[control] settings → uuid={client_uuid}")
            self._send_json({"settings": [{"uuid": client_uuid}]})

        elif self.path == "/RMBTControlServer/testRequest":
            token = f"{str(uuid.uuid4())}_token"
            test_uuid = str(uuid.uuid4())
            print(f"[control] testRequest → server={TEST_SERVER_HOST}:{TEST_SERVER_PORT} token={token[:20]}...")
            self._send_json({
                "test_token":           token,
                "test_uuid":            test_uuid,
                "open_test_uuid":       f"O{test_uuid}",
                "test_server_address":  TEST_SERVER_HOST,
                "test_server_port":     TEST_SERVER_PORT,
                "test_server_encryption": False,
                "test_duration":        7,
                "test_numthreads":      4,
                "test_wait":            0,
                "test_server_type":     "RMBThttp",
            })

        elif self.path == "/RMBTControlServer/result":
            print("[control] result received — OK")
            self._send_json({})

        else:
            self.send_error(404)


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8080), Handler)
    print(f"Mock control server listening on http://127.0.0.1:8080")
    print(f"Pointing to test server at {TEST_SERVER_HOST}:{TEST_SERVER_PORT}")
    server.serve_forever()
