"""One Linux process per call. Install limits before parsing or executing submitted Python."""
import ast
import ctypes
import errno
import importlib
import json
import os
import resource
import sys

MAX_BYTES = 128 * 1024


def restrict_syscalls():
    sc = ctypes.CDLL('libseccomp.so.2')
    sc.seccomp_init.argtypes = [ctypes.c_uint32]
    sc.seccomp_init.restype = ctypes.c_void_p
    sc.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    sc.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sc.seccomp_load.argtypes = [ctypes.c_void_p]
    sc.seccomp_release.argtypes = [ctypes.c_void_p]

    # Default EPERM, including open/openat, socket, clone, execve, ptrace, kill and namespace calls.
    policy = sc.seccomp_init(0x00050000 | errno.EPERM)
    if not policy:
        raise RuntimeError('seccomp_init failed')
    try:
        for name in ('read', 'write', 'close', 'brk', 'mmap', 'mremap', 'munmap', 'madvise',
                     'mprotect', 'futex', 'getrandom', 'clock_gettime', 'clock_nanosleep',
                     'nanosleep', 'getpid', 'gettid', 'rt_sigreturn', 'exit', 'exit_group'):
            nr = sc.seccomp_syscall_resolve_name(name.encode())
            if nr < 0 or sc.seccomp_rule_add(policy, 0x7fff0000, nr, 0):
                raise RuntimeError(f'cannot allow syscall {name}')
        if sc.seccomp_load(policy):
            raise RuntimeError('seccomp_load failed')
    finally:
        sc.seccomp_release(policy)


def main():
    for limit, hard in ((resource.RLIMIT_AS, 128 << 20), (resource.RLIMIT_CPU, 2),
                        (resource.RLIMIT_FSIZE, MAX_BYTES), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(limit, (hard, hard))
    # After lockdown the interpreter cannot load modules from disk. Preload common test helpers.
    for module in ('_strptime', 'base64', 'binascii', 'csv', 'datetime', 'decimal', 'hashlib',
                   'hmac', 'ipaddress', 'itertools', 'math', 'random', 're', 'secrets',
                   'statistics', 'string', 'time', 'urllib.parse', 'uuid', 'zlib'):
        importlib.import_module(module)
    # Separate the JSON response from user print() calls.
    output = os.dup(1)
    with open(os.devnull, 'wb') as devnull:
        os.dup2(devnull.fileno(), 1)
    if sys.argv[1:] == ['--check']:
        restrict_syscalls()
        return

    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    restrict_syscalls()
    try:
        req = json.loads(raw)
        tree = ast.parse(req['code'])
        name = req.get('name') or next(node.name for node in tree.body if isinstance(node, ast.FunctionDef))
        namespace = {'__name__': 'custom_function'}
        exec(compile(tree, '<custom function>', 'exec'), namespace)
        result = namespace[name](*req.get('args', []), **req.get('kwargs', {}))
        payload = json.dumps({'result': str(result) if req.get('display') else result}, ensure_ascii=False)
    except Exception as exc:
        payload = json.dumps({'error': (str(exc) or type(exc).__name__)[:2048]})
    data = payload.encode()
    if len(data) > MAX_BYTES - 1:
        data = json.dumps({'error': '函数输出超过 128 KiB。'}).encode()
    os.write(output, data)


if __name__ == '__main__':
    main()
