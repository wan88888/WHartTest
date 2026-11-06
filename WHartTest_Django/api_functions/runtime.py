"""Django-side client and registration of project custom functions."""
import ast
from functools import partial
import json
import os
import socket

MAX_BYTES = 128 * 1024


class SandboxUnavailable(RuntimeError):
    pass


def call_function(code, name, args, kwargs, *, display=False):
    request = {'code': code, 'name': name, 'args': args, 'kwargs': kwargs, 'display': display}
    try:
        message = json.dumps(request, ensure_ascii=False, allow_nan=False).encode() + b'\n'
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError('自定义函数参数需使用 JSON 类型。') from exc
    if len(message) > MAX_BYTES:
        raise ValueError('自定义函数输入超过 128 KiB。')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(6)
            sock.connect(os.environ.get('FUNCTION_SANDBOX_SOCKET', '/run/whart-functions/runner.sock'))
            sock.sendall(message)
            with sock.makefile('rb') as incoming:
                line = incoming.readline(MAX_BYTES + 1)
            if len(line) > MAX_BYTES or not line.endswith(b'\n'):
                raise ValueError('invalid sandbox response')
            response = json.loads(line)
            if isinstance(response, dict) and response.get('busy'):
                raise SandboxUnavailable('函数执行器繁忙，请稍后重试。')
            if not isinstance(response, dict) or not ('error' in response or 'result' in response):
                raise ValueError('invalid sandbox response')
    except (OSError, ValueError, RecursionError) as exc:
        raise SandboxUnavailable('函数隔离服务不可用或繁忙，请稍后重试。') from exc
    if 'error' in response:
        raise ValueError(str(response['error'])[:2048])
    return response['result']


def registered_functions(code):
    # Only inspect declarations here. Module-level code and function bodies run in the sandbox.
    try:
        tree = ast.parse(code)
    except (ValueError, SyntaxError, RecursionError):
        return {}
    return {node.name: partial(call_saved_function, code, node.name)
            for node in tree.body if isinstance(node, ast.FunctionDef)}


def call_saved_function(code, name, /, *args, **kwargs):
    return call_function(code, name, args, kwargs)


def load_custom_functions(project_id):
    from .models import ApiCustomFunction

    functions = {}
    for code in ApiCustomFunction.objects.filter(project_id=project_id, is_active=True).values_list('code', flat=True):
        functions.update(registered_functions(code))
    return functions
