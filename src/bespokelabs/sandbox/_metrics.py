"""Best-effort metric emission with no hard dependency.

A point goes to exactly one sink, chosen in this order:

1. A callable registered with ``set_metrics_sink(fn)``.  ``fn(name, value,
   tags)`` receives the metric name, a float and a ``dict[str, str]`` of tags.
   This is the hook for a consumer that already has its own metrics path (a
   Datadog HTTP client, OpenTelemetry, a test).  It runs synchronously on
   the caller's thread, after the operation it measures, so it should be
   fast and hand anything slow off to its own thread or queue.
2. A local Datadog Agent over DogStatsD, when ``DD_DOGSTATSD_SOCKET`` (a Unix
   datagram socket), ``DD_AGENT_HOST`` or ``DD_DOGSTATSD_HOST`` is set (UDP,
   port ``DD_DOGSTATSD_PORT``, default 8125).  Points are sent as
   distributions, and carry ``env`` / ``service`` from ``DD_ENV`` /
   ``DD_SERVICE`` when those are set.
3. Nothing.  With no sink configured, emission is a no-op.

Independently of the sink, every point is also written as one structured log
line on the ``bespokelabs.sandbox.metrics`` logger, so the data exists even
where no sink is configured.  Emission never raises, and the DogStatsD path
never blocks: a datagram is one non-blocking send with no reply.  If the
DogStatsD socket cannot be opened (the agent is not up yet), setup is retried
at most every ``_RETRY_SECS`` seconds, so metrics resume once it is.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping

metrics_logger = logging.getLogger("bespokelabs.sandbox.metrics")

MetricsSink = Callable[[str, float, Mapping[str, str]], None]

_DEFAULT_PORT = 8125
# Minimum time between attempts to open a DogStatsD socket that failed.
_RETRY_SECS = 30.0

_lock = threading.Lock()
_sink: MetricsSink | None = None
# Cached DogStatsD socket and the env it was built from, so a changed
# environment (tests, a late DD_AGENT_HOST) rebuilds it.  A failed setup is
# cached as None with the monotonic time it may next be retried.
_statsd: tuple[tuple, socket.socket | None, float] | None = None


def set_metrics_sink(sink: MetricsSink | None) -> None:
    """Route every metric to ``sink`` instead of DogStatsD; ``None`` resets."""
    global _sink
    with _lock:
        _sink = sink


def _statsd_key() -> tuple:
    return (
        os.environ.get("DD_DOGSTATSD_SOCKET") or "",
        os.environ.get("DD_AGENT_HOST")
        or os.environ.get("DD_DOGSTATSD_HOST")
        or "",
        os.environ.get("DD_DOGSTATSD_PORT") or "",
    )


def _port(raw: str) -> int:
    try:
        port = int(raw.strip()) if raw.strip() else _DEFAULT_PORT
    except ValueError:
        return _DEFAULT_PORT
    return port if 1 <= port <= 65535 else _DEFAULT_PORT


def _statsd_socket() -> socket.socket | None:
    global _statsd
    key = _statsd_key()
    with _lock:
        if _statsd is not None and _statsd[0] == key:
            if _statsd[1] is not None or time.monotonic() < _statsd[2]:
                return _statsd[1]
        if _statsd is not None and _statsd[1] is not None:
            try:
                _statsd[1].close()
            except OSError:
                pass
        sock: socket.socket | None = None
        path, host, port = key
        try:
            if path:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sock.setblocking(False)
                sock.connect(path)
            elif host:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setblocking(False)
                sock.connect((host, _port(port)))
        except OSError:
            metrics_logger.debug("DogStatsD socket unavailable", exc_info=True)
            sock = None
        _statsd = (key, sock, time.monotonic() + _RETRY_SECS)
        return sock


def _clean(value: str) -> str:
    # DogStatsD reserves ',', '|' and '#' in the tag section.
    return str(value).replace(",", "_").replace("|", "_").replace("#", "_")


def _datagram(name: str, value: float, tags: Mapping[str, str]) -> bytes:
    all_tags = dict(tags)
    for tag, env in (("env", "DD_ENV"), ("service", "DD_SERVICE")):
        if os.environ.get(env) and tag not in all_tags:
            all_tags[tag] = os.environ[env]
    line = f"{name}:{value:g}|d"
    if all_tags:
        line += "|#" + ",".join(
            f"{_clean(k)}:{_clean(v)}" for k, v in all_tags.items()
        )
    return line.encode()


def emit(
    name: str,
    value: float,
    tags: Mapping[str, str],
    *,
    log_level: int = logging.INFO,
) -> None:
    """Record one distribution point.  Never raises."""
    try:
        metrics_logger.log(
            log_level,
            "%s",
            json.dumps(
                {"metric": name, "value": value, "tags": dict(tags)},
                sort_keys=True,
            ),
        )
        with _lock:
            sink = _sink
        if sink is not None:
            sink(name, float(value), dict(tags))
            return
        sock = _statsd_socket()
        if sock is not None:
            sock.send(_datagram(name, float(value), tags))
    except Exception:  # noqa: BLE001 - metrics must never fail the caller
        metrics_logger.debug("Dropped metric %s", name, exc_info=True)
