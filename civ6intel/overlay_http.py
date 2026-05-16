from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def start_overlay_server(overlay_json: Path, *, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = overlay_handler(overlay_json)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Overlay JSON server: http://{host}:{port}/overlay.json")
    return server


def run_overlay_server(overlay_json: Path, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = ThreadingHTTPServer((host, port), overlay_handler(overlay_json))
    print(f"Overlay JSON server: http://{host}:{port}/overlay.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def overlay_handler(overlay_json: Path) -> type[BaseHTTPRequestHandler]:
    class OverlayHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/", "/overlay.json"}:
                self.send_error(404)
                return
            if not overlay_json.exists():
                body = json.dumps({"status": "WAITING", "newsText": "等待 overlay.json..."}, ensure_ascii=False)
            else:
                body = overlay_json.read_text(encoding="utf-8")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return OverlayHandler
