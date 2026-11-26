import socket
import threading
import time
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from utils.general.service_readiness import smtp_ready, woocommerce_ready


def test_smtp_probe_waits_for_delayed_greeting_and_does_not_send_early():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    seen = []
    def server():
        with listener:
            client, _ = listener.accept()
            with client:
                client.settimeout(.15)
                try:
                    seen.append(client.recv(512))
                except TimeoutError:
                    seen.append(b'no early command')
                client.sendall(b'220 delayed.test ESMTP ready\r\n')
                client.settimeout(2)
                seen.append(client.recv(512))
                client.sendall(b'250 OK\r\n')
                seen.append(client.recv(512))
                client.sendall(b'221 Bye\r\n')
    worker = threading.Thread(target=server)
    worker.start()
    try:
        assert smtp_ready('127.0.0.1', port, timeout=2)
    finally:
        worker.join(3)
    assert seen[0] == b'no early command'
    assert seen[1].strip().lower() == b'noop'
    assert seen[2].strip().lower() == b'quit'


def test_unavailable_smtp_fails_instead_of_reporting_ready():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    assert not smtp_ready('127.0.0.1', port, timeout=.2)


def test_wordpress_http_success_is_not_woocommerce_readiness():
    payload = {'namespaces': ['wp/v2']}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/wp-json/'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        assert not woocommerce_ready('127.0.0.1', server.server_port)
        payload['namespaces'].append('wc/v3')
        assert woocommerce_ready('127.0.0.1', server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_woocommerce_probe_uses_the_deployed_canonical_hostname():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'global_preparation/deploy_containers.sh').read_text()
    command = source.split('woo_ready() {', 1)[1].split('}', 1)[0]
    assert '--host localhost' in command

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers['Host'].startswith('127.0.0.1:'):
                self.send_response(302)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if self.path == '/wp-json/':
                self.wfile.write(json.dumps({'namespaces': ['wc/v3']}).encode())
            else:
                self.wfile.write(b'<html>WordPress home</html>')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        assert not woocommerce_ready('127.0.0.1', server.server_port)
        assert woocommerce_ready('localhost', server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
