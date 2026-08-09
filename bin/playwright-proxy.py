#!/usr/bin/env python3
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TARGET_HOST = os.environ.get("PLAYWRIGHT_PROXY_TARGET_HOST", "next-to-you-ui")
TARGET_PORT = int(os.environ.get("PLAYWRIGHT_PROXY_TARGET_PORT", "80"))
LISTEN_PORT = int(os.environ.get("PLAYWRIGHT_PROXY_LISTEN_PORT", "4200"))
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def proxy(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else None
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP and name.lower() != "host"
        }
        headers["Host"] = TARGET_HOST

        connection = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT, timeout=30)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP:
                    self.send_header(name, value)
            self.end_headers()
            while chunk := response.read(64 * 1024):
                self.wfile.write(chunk)
        finally:
            connection.close()

    do_GET = proxy
    do_HEAD = proxy
    do_POST = proxy
    do_PUT = proxy
    do_PATCH = proxy
    do_DELETE = proxy
    do_OPTIONS = proxy

    def log_message(self, _format, *_args):
        return


ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler).serve_forever()
