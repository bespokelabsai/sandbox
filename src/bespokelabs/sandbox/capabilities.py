"""Declared per-backend capabilities and the checks built on them.

Every backend client class carries one ``capabilities`` attribute.  It is
the single source of truth for what that backend can do: ``Sandbox`` checks
requests against it before building the adapter (so the answer never
depends on whether a provider SDK is installed), and the tests check it
against the adapters and the README feature matrix.
"""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
    UnsupportedOptionWarning,
)

#: ``Sandbox(...)`` creation options whose support varies by backend.
#: ``dockerfile=`` and ``network=`` have their own capability fields.
CREATE_OPTIONS = frozenset(
    {
        "cpu",
        "memory_mb",
        "disk_mb",
        "gpu",
        "timeout_secs",
        "image",
        "env_vars",
        "allow_internet",
        "app_name",
        "template",
        "snapshot_id",
        "workdir",
        "backend_options",
    }
)


@dataclass(frozen=True)
class BackendCapabilities:
    """What one backend supports, declared on its client class.

    Attributes:
        sessions: Creates interactive sandboxes.  ``False`` for job-only
            backends such as ``gcp_batch``.
        jobs: Runs detached jobs (``SandboxClient.submit_job`` and friends).
        options: The ``Sandbox(...)`` creation options the backend applies.
            An option outside this set is ignored or rejected by the adapter.
        dockerfile: Builds from ``dockerfile=``.
        network_modes: ``NetworkPolicy`` modes enforced at creation.
        network_option_keys: ``backend_options`` keys that control networking
            and would override ``network=``.
        set_network: Modes ``Sandbox.set_network()`` can switch to on a
            running sandbox; empty when the policy is fixed at creation.
        expose_port: Implements ``Sandbox.expose_port()``.
        snapshot: Implements ``Sandbox.snapshot()``.
        resume: Implements ``session_state()`` and ``SandboxClient.resume()``.

    """

    sessions: bool = True
    jobs: bool = False
    options: frozenset[str] = frozenset()
    dockerfile: bool = False
    network_modes: frozenset[str] = frozenset({"public"})
    network_option_keys: frozenset[str] = frozenset()
    set_network: frozenset[str] = frozenset()
    expose_port: bool = False
    snapshot: bool = False
    resume: bool = False

    def __post_init__(self) -> None:
        unknown = set(self.options) - CREATE_OPTIONS
        if unknown:
            raise ValueError(
                f"Unknown creation options: {', '.join(sorted(unknown))}"
            )

    def supports_option(self, name: str) -> bool:
        return name in self.options


# Assumed for a client that declares nothing (a test double or a client
# from outside this package), matching the behaviour before capabilities
# were declared: options and session operations are left to the adapter,
# while Dockerfile builds and non-public network policies stay refused.
# Every client in BACKENDS declares its own.
UNDECLARED = BackendCapabilities(
    options=CREATE_OPTIONS,
    set_network=frozenset({"public", "none", "allowlist"}),
    expose_port=True,
    snapshot=True,
    resume=True,
)


def capabilities_of(client: object) -> BackendCapabilities:
    """Return the capabilities declared by a backend client or its class."""
    caps = getattr(client, "capabilities", None)
    if isinstance(caps, BackendCapabilities):
        return caps
    return UNDECLARED


def backend_capabilities(backend: str) -> BackendCapabilities:
    """Return what *backend* supports, without needing its SDK installed."""
    from bespokelabs.sandbox.backends import BACKENDS

    name = backend.lower().strip()
    if name not in BACKENDS:
        raise SandboxConfigurationError(
            f"Unknown backend '{name}'. Choose from: {', '.join(BACKENDS)}",
            context={"backend": name},
        )
    return capabilities_of(BACKENDS[name])


def explicit_options(**values: Any) -> set[str]:
    """Names of the creation options a caller actually set.

    ``None`` and empty mappings mean "not set".  ``allow_internet=True`` is
    every backend's behaviour, so only ``False`` asks for anything.
    """
    names = set()
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, Mapping) and not value:
            continue
        if name == "allow_internet" and value:
            continue
        names.add(name)
    return names


def check_options(
    backend: str,
    capabilities: BackendCapabilities,
    requested: set[str],
    *,
    strict: bool | None,
) -> None:
    """Report creation options *backend* would not apply.

    ``strict=True`` raises FeatureNotSupportedError, ``False`` accepts the
    options silently, and ``None`` (the default) warns with
    UnsupportedOptionWarning.  The default will become raising.
    """
    unsupported = sorted(
        name for name in requested if not capabilities.supports_option(name)
    )
    if not unsupported or strict is False:
        return
    names = ", ".join(f"{name}=" for name in unsupported)
    message = f"Backend '{backend}' does not support {names}"
    if strict:
        raise FeatureNotSupportedError(message, backend=backend, op="create")
    warnings.warn(
        f"{message}. This will raise FeatureNotSupportedError in a future "
        "release; pass strict=True to raise now, or strict=False to accept "
        "the backend's behaviour without this warning.",
        UnsupportedOptionWarning,
        stacklevel=_caller_stacklevel(),
    )


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep


def _caller_stacklevel() -> int:
    """Stack level of the first frame outside this package.

    ``Sandbox(...)``, ``SandboxClient.create()`` and the async wrappers all
    reach the check at different depths; the warning should name the
    caller's line in each case.
    """
    frame = sys._getframe(1)
    level = 1
    while frame is not None and os.path.abspath(
        frame.f_code.co_filename
    ).startswith(_PACKAGE_DIR):
        frame = frame.f_back
        level += 1
    return level
