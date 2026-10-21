"""Modal sandbox backend."""

from __future__ import annotations

import pathlib

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import (
    FileInfo,
    NetworkPolicy,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)


class ModalClient:
    """Factory for Modal sandboxes."""

    network_modes = frozenset({"public", "none", "allowlist"})
    # cidr_allowlist is the deprecated name of outbound_cidr_allowlist.
    network_option_keys = frozenset(
        {
            "block_network",
            "outbound_cidr_allowlist",
            "outbound_domain_allowlist",
            "cidr_allowlist",
        }
    )

    def __init__(self) -> None:
        try:
            import modal  # type: ignore[import-untyped]
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Modal SDK not installed. Run: pip install bespokelabs-sandbox[modal]"
            ) from exc
        self._modal = modal

    def create(self, config: SandboxConfig) -> ModalSession:
        modal = self._modal
        try:
            app_name = config.app_name or "sandbox-sdk"
            app = modal.App.lookup(app_name, create_if_missing=True)

            create_kwargs: dict = {
                "app": app,
                "timeout": config.timeout_secs,
                "cpu": config.cpu,
                "memory": config.memory_mb,
            }

            if config.gpu is not None:
                create_kwargs["gpu"] = config.gpu

            if config.image:
                create_kwargs["image"] = modal.Image.from_registry(config.image)

            if config.snapshot_id:
                create_kwargs["image"] = modal.Image.from_id(config.snapshot_id)

            if config.env_vars:
                create_kwargs["secrets"] = [
                    modal.Secret.from_dict(config.env_vars)
                ]

            create_kwargs.update(_network_kwargs(config.network))

            if config.backend_options:
                create_kwargs.update(config.backend_options)

            sandbox = modal.Sandbox.create(**create_kwargs)
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to create Modal sandbox: {exc}"
            ) from exc

        return ModalSession(sandbox=sandbox)

    def resume(self, data: dict) -> ModalSession:
        try:
            sandbox = self._modal.Sandbox.from_id(data["sandbox_id"])
        except Exception as exc:
            raise SandboxCreationError(
                f"Cannot resume Modal sandbox '{data.get('sandbox_id')}': {exc}"
            ) from exc
        return ModalSession(sandbox=sandbox)


def _network_kwargs(policy: NetworkPolicy | None) -> dict:
    """Map a policy onto ``Sandbox.create`` keyword arguments."""
    if policy is None or policy.mode == "public":
        return {}
    if policy.mode == "none":
        return {"block_network": True}
    kwargs: dict = {}
    if policy.cidrs:
        kwargs["outbound_cidr_allowlist"] = list(policy.cidrs)
    if policy.domains:
        kwargs["outbound_domain_allowlist"] = list(policy.domains)
    return kwargs


class ModalSession:
    """One live Modal sandbox."""

    def __init__(self, *, sandbox: object) -> None:
        self._sandbox: object = sandbox

    @property
    def provider_resource_id(self) -> str | None:
        """Return the Modal sandbox object ID while it is attached."""
        value = getattr(self._sandbox, "object_id", None)
        return str(value) if value is not None else None

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        try:
            process = self._sandbox.exec(language, "-c", code)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.wait()
            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal code execution failed: {exc}"
            ) from exc

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        try:
            cmd_parts = (
                ["bash", "-c", command] if not args else [command] + args
            )
            process = self._sandbox.exec(*cmd_parts)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.wait()
            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal command execution failed: {exc}"
            ) from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._sandbox.ls(path)
            return [
                FileInfo(
                    path=f"{path.rstrip('/')}/{e}"
                    if isinstance(e, str)
                    else getattr(e, "path", str(e)),
                    is_dir=getattr(e, "is_dir", False)
                    if not isinstance(e, str)
                    else False,
                )
                for e in entries
            ]
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal list_files failed: {exc}"
            ) from exc

    def read_file(self, path: str) -> bytes:
        try:
            with self._sandbox.open(path, "rb") as f:
                return f.read()
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal read_file failed: {exc}"
            ) from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            with self._sandbox.open(path, "wb") as f:
                f.write(data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal write_file failed: {exc}"
            ) from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            with self._sandbox.open(remote_path, "wb") as f:
                f.write(data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal upload_file failed: {exc}"
            ) from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            with self._sandbox.open(remote_path, "rb") as f:
                data = f.read()
            pathlib.Path(local_path).write_bytes(data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal download_file failed: {exc}"
            ) from exc

    def set_network(self, policy: NetworkPolicy) -> None:
        """Replace the outbound policy via Modal's experimental API."""
        setter = getattr(
            self._sandbox, "_experimental_set_outbound_network_policy", None
        )
        if setter is None:
            raise FeatureNotSupportedError(
                "This Modal SDK cannot change the network policy of a "
                "running sandbox",
                backend="modal",
                op="set_network",
            )
        if policy.mode == "none":
            raise FeatureNotSupportedError(
                "Modal blocks all egress only at creation; pass "
                "network=NetworkPolicy(mode='none') to the constructor",
                backend="modal",
                op="set_network",
            )
        kwargs: dict = {}
        if policy.mode == "allowlist":
            kwargs = {
                "outbound_cidr_allowlist": list(policy.cidrs) or None,
                "outbound_domain_allowlist": list(policy.domains) or None,
            }
        try:
            setter(**kwargs)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal set_network failed: {exc}"
            ) from exc

    def snapshot(self) -> SnapshotInfo:
        try:
            image = self._sandbox.snapshot_filesystem()
            return SnapshotInfo(
                snapshot_id=getattr(image, "object_id", str(image)),
                backend="modal",
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal snapshot failed: {exc}"
            ) from exc

    def session_state(self) -> dict:
        return {"sandbox_id": self._sandbox.object_id}

    def destroy(self) -> None:
        try:
            if self._sandbox:
                self._sandbox.terminate()
        except Exception:
            pass
        self._sandbox = None
