"""Passive integration prerequisites. No GTK, service starts or input events."""
from __future__ import annotations

import ast
import errno
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import subprocess
import time

from . import injector, shortcut


def _socket_state(path, remaining):
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            return 'stale'
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(remaining())
            sock.connect(str(path))  # No datagram, clipboard access or key events.
        return 'reachable'
    except FileNotFoundError:
        return 'missing'
    except PermissionError:
        return 'permission_denied'
    except (TimeoutError, socket.timeout):
        return 'timeout'
    except OSError as exc:
        return 'stale' if exc.errno in {errno.ECONNREFUSED, errno.ENOTSOCK} else 'unknown'


def _binding(cfg, remaining):
    if shortcut.manual_shortcut_required():
        return 'manual'
    if not cfg.get('shortcut'):
        return 'disabled'

    def read(schema, key):
        cp = subprocess.run(['gsettings', 'get', schema, key], capture_output=True,
                            text=True, check=True, timeout=remaining())
        return ast.literal_eval(cp.stdout.removeprefix('@as ').strip())

    paths = read(shortcut.SCHEMA, 'custom-keybindings')
    if not isinstance(paths, list) or shortcut.KEY not in paths:
        return 'missing'
    schema = f'{shortcut.SCHEMA}.custom-keybinding:{shortcut.KEY}'
    binding = read(schema, 'binding')
    command = read(schema, 'command')
    if not isinstance(binding, str) or not isinstance(command, str):
        return 'unknown'
    # GSettings command quoting can differ while identifying the same executable.
    if shlex.split(command) != shlex.split(shortcut.manual_command()):
        return 'mismatch'
    normalize = lambda value: value.replace('<Primary>', '<Control>').lower()
    return 'matched' if normalize(binding) == normalize(str(cfg['shortcut'])) else 'mismatch'


def collect_health(cfg: dict, *, timeout=3.0) -> dict:
    """Bound subprocess/socket waits by one deadline; filesystem probes are local.

    `reachable` and `found` describe prerequisites, never verified target delivery.
    A helper can own uinput even when this process cannot open that device.
    """
    deadline = time.monotonic() + max(0, timeout)
    result = dict(checked_at=time.time(), clipboard='unknown', client='unknown',
                  helper='unknown', socket='', uinput='unknown', shortcut='unknown',
                  delivery='unknown', reason='unknown', complete=False)

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('Integration check deadline exceeded')
        return left

    try:
        remaining()
        result['clipboard'] = ('missing' if not shutil.which('wl-copy') else
                               'found' if os.environ.get('WAYLAND_DISPLAY') else 'no_session')
        result['client'] = 'found' if injector.ydotool_command() else 'missing'
        best = ('missing', '')
        for path in injector.ydotool_socket_candidates():
            remaining()
            state = _socket_state(path, remaining)
            if state == 'reachable':
                best = (state, str(path))
                break
            if best[0] == 'missing' and state != 'missing':
                best = (state, str(path))
        result['helper'], result['socket'] = best
        remaining()
        try:
            Path('/dev/uinput').stat()
            result['uinput'] = 'accessible' if os.access('/dev/uinput', os.W_OK) else 'permission_denied'
        except FileNotFoundError:
            result['uinput'] = 'missing'
        except PermissionError:
            result['uinput'] = 'permission_denied'
        except OSError:
            result['uinput'] = 'unknown'
        try:
            result['shortcut'] = _binding(cfg, remaining)
        except PermissionError:
            result['shortcut'] = 'permission_denied'
        except (TimeoutError, subprocess.TimeoutExpired):
            result['shortcut'] = 'timeout'
            raise TimeoutError
        except (OSError, subprocess.SubprocessError, ValueError, SyntaxError):
            result['shortcut'] = 'unknown'
        remaining()
        result['complete'] = True
    except TimeoutError:
        result['reason'] = 'timeout'
        return result

    if result['clipboard'] != 'found':
        result.update(delivery='needs_attention', reason='clipboard_' + result['clipboard'])
    elif cfg.get('paste_mode') == 'copy':
        result.update(delivery='copy_only', reason='copy_only')
    elif result['client'] != 'found':
        result.update(delivery='copy_only', reason='client_missing')
    elif result['helper'] == 'reachable':
        result.update(delivery='helper_available', reason='prerequisites')
    else:
        result.update(delivery='copy_only', reason='helper_' + result['helper'])
    return result
