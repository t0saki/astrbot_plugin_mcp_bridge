"""Boot a real AstrBot core (no dashboard) with outbound network blocked.

Usage (cwd = AstrBot source tree, ASTRBOT_ROOT = fresh data root):

    python boot_astrbot.py

Only loopback connections are permitted: hostname lookups other than
localhost and connects to non-loopback addresses raise, so telemetry, model
metadata or t2i endpoint fetches fail fast instead of reaching the internet.
"""

import asyncio
import ipaddress
import os
import socket
import sys

_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}
_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode()
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard_getaddrinfo(host, *args, **kwargs):
    if not _is_local(host):
        raise socket.gaierror(socket.EAI_NONAME, f"e2e network guard: lookup of {host!r} blocked")
    return _real_getaddrinfo(host, *args, **kwargs)


def _check_addr(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address[0]):
        raise ConnectionRefusedError(f"e2e network guard: connect to {address[0]!r} blocked")


def _guard_connect(self, address):
    _check_addr(self, address)
    return _real_connect(self, address)


def _guard_connect_ex(self, address):
    _check_addr(self, address)
    return _real_connect_ex(self, address)


socket.getaddrinfo = _guard_getaddrinfo
socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"

sys.path.insert(0, os.getcwd())

import runtime_bootstrap  # noqa: E402

runtime_bootstrap.initialize_runtime_bootstrap()

from astrbot.core import LogBroker, LogManager, db_helper, logger  # noqa: E402
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle  # noqa: E402

import main as astrbot_main  # noqa: E402  (AstrBot's main.py: check_env only)


async def run() -> None:
    log_broker = LogBroker()
    LogManager.set_queue_handler(logger, log_broker)
    core = AstrBotCoreLifecycle(log_broker, db_helper)
    await core.initialize()
    logger.info("E2E: AstrBot core initialized")
    try:
        await core.start()
    finally:
        await core.stop()


if __name__ == "__main__":
    astrbot_main.check_env()
    asyncio.run(run())
