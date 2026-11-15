# python
# Copyright (c) 2026 Leonard Billeke
# Copyright (c) 2026 modo-tools contributors
#
# Adapted from modo-mcp 0.3.0, kit/mcp_bridge/Scripts/python_modules/
# mcpbridge/authtoken.py, under the MIT License:
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""The shared secret between the kit and the server.

The kit and the server run as the same user on the same PC, so the kit writes
a random token to ~/.modo_tools/token and the server reads it from there.
Nothing is copied into a config. Anyone who can read the file can drive Modo,
so it is created owner-only (0600) on macOS and Linux. On Windows those bits
do nothing; the file is protected by the user profile it lives in.
"""

import os
import secrets


def load_or_create(path):
    """The token in the file, or a new one written there once."""
    existing = read(path)
    if existing:
        return existing
    value = secrets.token_urlsafe(32)
    _write(path, value)
    return value


def read(path):
    try:
        with open(path, "r") as handle:
            value = handle.read().strip()
        return value or None
    except (OSError, IOError):
        return None


def _write(path, value):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    # Owner-only from the moment the file exists, so there is no window where
    # another account can read it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
