from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxPreset:
    """A predefined sandbox configuration with setup commands.

    Attributes:
        name: Human-readable preset name.
        description: What this preset provides.
        image: Pre-built OCI container image for docker/daytona/modal backends
            (skips setup_commands if the backend supports images).
        tensorlake_image: Tensorlake project-scoped image name. Tensorlake
            doesn't accept arbitrary OCI URLs, so this is a separate field.
            Users register a project image with `tl sbx image build` (our
            Dockerfiles under images/ can be used as-is) and then set this.
            When set, setup_commands are skipped on the Tensorlake backend.
        setup_commands: Shell commands run after sandbox creation (in order).
        backend_setup_commands: Backend-specific setup commands. When present
            for a backend, these replace setup_commands on that backend.
        cpu: Minimum recommended vCPUs.
        memory_mb: Minimum recommended RAM in MB.
        timeout_secs: Recommended timeout.
        env_vars: Environment variables to set.
        allow_internet: Whether internet access is needed (e.g. for installs).
    """

    name: str
    description: str
    image: str | None = None
    tensorlake_image: str | None = None
    setup_commands: list[str] = field(default_factory=list)
    backend_setup_commands: dict[str, list[str]] = field(default_factory=dict)
    cpu: float = 1.0
    memory_mb: int = 1024
    timeout_secs: int = 600
    env_vars: dict[str, str] = field(default_factory=dict)
    allow_internet: bool = True


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, SandboxPreset] = {}


def register_preset(preset: SandboxPreset) -> SandboxPreset:
    """Register a preset so it can be referenced by name."""
    PRESETS[preset.name] = preset
    return preset


def get_preset(name: str) -> SandboxPreset:
    """Look up a preset by name. Raises KeyError if not found."""
    if name not in PRESETS:
        available = ", ".join(sorted(PRESETS)) or "(none)"
        raise KeyError(f"Unknown preset '{name}'. Available presets: {available}")
    return PRESETS[name]


# -- Constants -------------------------------------------------------------

IMAGE_REGISTRY = "ghcr.io/bespokelabsai/sandbox"

# Pinned tag for preset images. Bump to cut a new immutable image set,
# then run the build-images workflow_dispatch with the same tag value.
# Never use ":latest" here — it's not reproducible.
#
# v2: every preset image now installs git, so git_repo= works on all of
#     them (v1 images were slim-based and lacked git).
PRESET_IMAGE_TAG = "v2"

# -- Agent presets ---------------------------------------------------------

register_preset(SandboxPreset(
    name="claude-code",
    description="Sandbox with Claude Code (Anthropic CLI) installed",
    image=f"{IMAGE_REGISTRY}/claude-code:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "npm install -g @anthropic-ai/claude-code",
    ],
    backend_setup_commands={
        "tensorlake": [
            "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g @anthropic-ai/claude-code",
        ],
    },
    memory_mb=2048,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="codex",
    description="Sandbox with Codex CLI installed",
    image=f"{IMAGE_REGISTRY}/codex:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "npm install -g @openai/codex",
    ],
    backend_setup_commands={
        "tensorlake": [
            "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g @openai/codex",
        ],
    },
    memory_mb=2048,
    timeout_secs=1800,
))
