"""Broker for the dedicated, networkless function container. Only JSON crosses the socket."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading

MAX_BYTES = 128 * 1024
SOCKET_PATH = '/run/whart-functions/runner.sock'
WORKER = Path(__file__).with_name('worker.py')


class Handler(socketserver.StreamRequestHandler):
    timeout = 5

    def handle(self):
        request = self.rfile.readline(MAX_BYTES + 1)
        if len(request) > MAX_BYTES or not request.endswith(b'\n'):
            self.reply({'error': '函数参数过大或格式错误。'})
            return

        # A regular file on container tmpfs allows RLIMIT_FSIZE to bound even raw fd writes.
        with tempfile.TemporaryFile() as output:
            try:
                completed = subprocess.run(
                    [sys.executable, '-I', '-S', '-B', str(WORKER)],
                    input=request, stdout=output, stderr=subprocess.DEVNULL,
                    env={}, close_fds=True, timeout=4, check=False,
                )
            except subprocess.TimeoutExpired:
                self.reply({'error': '函数执行超时。'})
                return
            output.seek(0)
            result = output.read(MAX_BYTES + 1)
        if completed.returncode or not result or len(result) > MAX_BYTES:
            self.reply({'error': '函数执行被隔离器终止，或超过资源限制。'})
            return
        try:
            self.reply(json.loads(result))
        except (ValueError, RecursionError):
            self.reply({'error': '函数输出格式错误。'})

    def reply(self, payload):
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode() + b'\n')


class SandboxServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # Bound both worker memory and waiting Django requests. No unbounded thread/child queue.
    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'{"busy":true}\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


if __name__ == '__main__':
    # No requests accepted if the host kernel cannot enforce the syscall whitelist.
    subprocess.run([sys.executable, '-I', '-S', '-B', str(WORKER), '--check'], check=True)
    Path(SOCKET_PATH).unlink(missing_ok=True)
    with SandboxServer(SOCKET_PATH, Handler) as server:
        os.chmod(SOCKET_PATH, 0o666)
        server.serve_forever()
