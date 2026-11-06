"""Run inside the sandbox image with the same Docker restrictions as production."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from server import Handler, SandboxServer


class SandboxBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls.tmp.name) / 'test.sock')
        cls.broker = SandboxServer(cls.path, Handler)
        cls.thread = threading.Thread(target=cls.broker.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.broker.shutdown()
        cls.broker.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def call(self, body, **kwargs):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(self.path)
            message = {'code': 'def func(**kw):\n    ' + body, 'kwargs': kwargs}
            client.sendall(json.dumps(message).encode() + b'\n')
            with client.makefile('rb') as incoming:
                return json.loads(incoming.readline())

    def test_full_pool_returns_busy(self):
        with self.broker.slots, self.broker.slots:
            self.assertTrue(self.call('return 1')['busy'])
        self.assertEqual(self.call('return 2'), {'result': 2})

    def test_standard_library_and_json(self):
        self.assertEqual(self.call('import hashlib; return [kw["x"], hashlib.md5(b"a").hexdigest()]', x=3),
                         {'result': [3, '0cc175b9c0f1b6a831c399e269772661']})

    def test_file_access_fork_and_network_denied(self):
        for source in ['return open("/etc/passwd").read()', 'import os; return os.fork()',
                       'import _socket; return _socket.socket()',
                       'import os; return os.open("/proc/1/environ", 0)']:
            with self.subTest(source=source):
                self.assertIn('error', self.call(source))
        self.assertIsNone(self.call('import os; return os.environ.get("DJANGO_SECRET_KEY")')['result'])

    def test_timeout_memory_and_output_limits(self):
        for source in ['import time; time.sleep(30)', 'return bytes(512 * 1024**2)',
                       'return "a" * 300000']:
            with self.subTest(source=source):
                self.assertIn('error', self.call(source))
        self.assertEqual(self.call('return 42'), {'result': 42})


if __name__ == '__main__':
    unittest.main()
