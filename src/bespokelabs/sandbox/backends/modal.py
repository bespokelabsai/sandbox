"""Modal sandbox backend."""

from __future__ import annotations

import inspect
import pathlib

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import (
    ExposedPort,
    FileInfo,
    NetworkPolicy,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)


class ModalClient:
    """Factory for Modal sandboxes."""

    supports_dockerfile = True
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

            if config.dockerfile:
                create_kwargs["image"] = _image_from_dockerfile(modal, config)

            if config.snapshot_id:
                create_kwargs["image"] = modal.Image.from_id(config.snapshot_id)

            if config.env_vars:
                create_kwargs["secrets"] = [
                    modal.Secret.from_dict(config.env_vars)
                ]

            network_kwargs = _network_kwargs(config.network)
            if network_kwargs:
                _require_create_kwargs(modal, network_kwargs)
            create_kwargs.update(network_kwargs)

            if config.backend_options:
                create_kwargs.update(config.backend_options)

            sandbox = modal.Sandbox.create(**create_kwargs)
        except FeatureNotSupportedError:
            raise
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


def _image_from_dockerfile(modal: object, config: SandboxConfig) -> object:
    """Build ``Image.from_dockerfile`` with the build context.

    ``context_dir`` arrived in modal 0.74; older SDKs only take a
    ``context_mount``, so fail clearly instead of with a TypeError.
    """
    from_dockerfile = modal.Image.from_dockerfile  # type: ignore[attr-defined]
    try:
        params = inspect.signature(from_dockerfile).parameters
    except (TypeError, ValueError):
        params = None
    if params is not None and not (
        "context_dir" in params
        or any(p.kind is p.VAR_KEYWORD for p in params.values())
    ):
        raise FeatureNotSupportedError(
            "Building from a Dockerfile on Modal needs modal>=0.74 "
            "(Image.from_dockerfile(context_dir=...)); upgrade modal",
            backend="modal",
            op="create",
        )
    return from_dockerfile(config.dockerfile, context_dir=config.context_dir)


def _require_create_kwargs(modal: object, kwargs: dict) -> None:
    """Refuse a network policy this Modal SDK's ``Sandbox.create`` can't take.

    The outbound allowlist arguments are newer than the ``modal>=0.60``
    floor; an older SDK would reject them with a TypeError, so fail as
    unsupported instead.
    """
    try:
        params = inspect.signature(
            modal.Sandbox.create  # type: ignore[attr-defined]
        ).parameters
    except (TypeError, ValueError):
        return
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return
    missing = sorted(k for k in kwargs if k not in params)
    if missing:
        raise FeatureNotSupportedError(
            "This Modal SDK's Sandbox.create does not accept "
            f"{', '.join(missing)}, so it cannot enforce this network "
            "policy; upgrade modal",
            backend="modal",
            op="create",
        )


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

    def expose_port(self, port: int) -> ExposedPort:
        """Return the tunnel URL for a port opened at creation."""
        try:
            tunnels = self._sandbox.tunnels()
        except Exception as exc:
            raise SandboxExecutionError(
                f"Modal tunnel lookup failed: {exc}"
            ) from exc
        url = _tunnel_url(tunnels, port)
        if url is None:
            raise SandboxConfigurationError(
                f"Modal opens ports only at creation; pass "
                f"backend_options={{'encrypted_ports': [{port}]}}",
                backend="modal",
                op="expose_port",
            )
        return ExposedPort(port=port, url=url)

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


def _tunnel_url(tunnels: object, port: int) -> str | None:
    """Find *port*'s URL in a ``Sandbox.tunnels()`` result.

    modal>=0.64.153 returns ``{port: Tunnel}``; earlier releases return a
    list of ``TunnelData`` protos, whose URL we build the way ``Tunnel.url``
    does.
    """
    if isinstance(tunnels, dict):
        tunnel = tunnels.get(port)
        return None if tunnel is None else tunnel.url
    for data in tunnels:  # type: ignore[attr-defined]
        if data.container_port == port:
            url = f"https://{data.host}"
            return url if data.port == 443 else f"{url}:{data.port}"
    return None
