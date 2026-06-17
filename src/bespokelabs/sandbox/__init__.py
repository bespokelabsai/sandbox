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

from bespokelabs.sandbox._transfer import build_files_map
from bespokelabs.sandbox.agents import (
    AgentCapability,
    AgentContext,
    AgentInputMode,
    AgentPlacement,
    AgentSession,
    AgentSpec,
    InsideAgentSpec,
    OutsideAgentSpec,
)
from bespokelabs.sandbox.aio import AsyncSandbox, AsyncSandboxClient
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    CommandFailedError,
    ErrorCode,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
    WorkspaceError,
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
from bespokelabs.sandbox.workspace import (
    File,
    GitRepo,
    LocalDir,
    LocalFile,
    Manifest,
    WorkspaceEntry,
)

__all__ = [
    "Sandbox",
    "SandboxClient",
    "AsyncSandbox",
    "AsyncSandboxClient",
    "AgentSpec",
    "AgentSession",
    "AgentContext",
    "InsideAgentSpec",
    "OutsideAgentSpec",
    "AgentPlacement",
    "AgentCapability",
    "AgentInputMode",
    "SandboxPreset",
    "SandboxConfig",
    "SandboxResult",
    "SandboxSessionState",
    "FileInfo",
    "SnapshotInfo",
    "build_files_map",
    "Manifest",
    "WorkspaceEntry",
    "File",
    "LocalFile",
    "LocalDir",
    "GitRepo",
    "SandboxError",
    "SandboxConfigurationError",
    "SandboxCreationError",
    "SandboxExecutionError",
    "CommandFailedError",
    "SandboxTimeoutError",
    "SandboxConnectionError",
    "SandboxNotFoundError",
    "BackendNotInstalledError",
    "FeatureNotSupportedError",
    "WorkspaceError",
    "ErrorCode",
    "json_schema",
]
