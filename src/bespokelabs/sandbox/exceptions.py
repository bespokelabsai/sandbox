from __future__ import annotations


class SandboxError(Exception):
    """Base exception for all sandbox errors."""


class SandboxCreationError(SandboxError):
    """Sandbox creation failed."""


class SandboxExecutionError(SandboxError):
    """Code or command execution failed."""


class BackendNotInstalledError(SandboxError):
    """Required pip package for a backend is not installed."""


class FeatureNotSupportedError(SandboxError):
    """The requested feature is not supported by this backend."""
