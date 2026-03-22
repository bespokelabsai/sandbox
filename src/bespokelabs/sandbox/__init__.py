"""bespokelabs-sandbox - OpenRouter for Sandboxes.

Unified Python API for cloud sandbox providers.
Supports Daytona, Tensorlake, Modal, and E2B as interchangeable backends.

Usage:
    from bespokelabs.sandbox import Sandbox

    with Sandbox("daytona") as sb:
        result = sb.execute_code('print("hello")')
        print(result.stdout)
"""

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.presets import SandboxPreset
from bespokelabs.sandbox.sandbox import Sandbox
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo

__all__ = [
    "Sandbox",
    "SandboxPreset",
    "SandboxConfig",
    "SandboxResult",
    "FileInfo",
    "SnapshotInfo",
    "SandboxError",
    "SandboxCreationError",
    "SandboxExecutionError",
    "BackendNotInstalledError",
    "FeatureNotSupportedError",
]
