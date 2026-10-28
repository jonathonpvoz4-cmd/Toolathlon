import socket
import threading
import time

from utils.general.service_readiness import smtp_ready


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
