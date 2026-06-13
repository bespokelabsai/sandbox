"""bespokelabs-sandbox - OpenRouter for Sandboxes.

Unified Python API for cloud sandbox providers.
Supports Local, Safehouse, Docker, Ray, Daytona, Tensorlake, Modal, and E2B
as interchangeable backends.

Usage:
    from bespokelabs.sandbox import Sandbox

    with Sandbox("safehouse") as sb:
        result = sb.execute_code('print("hello")')
        print(result.stdout)
"""

from bespokelabs.sandbox.aio import AsyncSandbox, AsyncSandboxClient
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.presets import SandboxPreset
from bespokelabs.sandbox.sandbox import Sandbox, SandboxClient, json_schema
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SandboxSessionState,
    SnapshotInfo,
)

__all__ = [
    "Sandbox",
    "SandboxClient",
    "AsyncSandbox",
    "AsyncSandboxClient",
    "SandboxPreset",
    "SandboxConfig",
    "SandboxResult",
    "SandboxSessionState",
    "FileInfo",
    "SnapshotInfo",
    "SandboxError",
    "SandboxCreationError",
    "SandboxExecutionError",
    "BackendNotInstalledError",
    "FeatureNotSupportedError",
    "json_schema",
]
