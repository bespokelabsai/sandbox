from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxPreset:
    """A predefined sandbox configuration with setup commands.

    Attributes:
        name: Human-readable preset name.
        description: What this preset provides.
        setup_commands: Shell commands run after sandbox creation (in order).
        cpu: Minimum recommended vCPUs.
        memory_mb: Minimum recommended RAM in MB.
        timeout_secs: Recommended timeout.
        env_vars: Environment variables to set.
        allow_internet: Whether internet access is needed (e.g. for installs).
    """

    name: str
    description: str
    setup_commands: list[str] = field(default_factory=list)
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


# -- Built-in presets ------------------------------------------------------

register_preset(SandboxPreset(
    name="claude-code",
    description="Sandbox with Claude Code (Anthropic CLI) installed",
    setup_commands=[
        "npm install -g @anthropic-ai/claude-code",
    ],
    memory_mb=2048,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="python-data-science",
    description="Python with numpy, pandas, matplotlib, scikit-learn",
    setup_commands=[
        "pip install numpy pandas matplotlib scikit-learn",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="node",
    description="Node.js environment with npm",
    setup_commands=[
        "node --version && npm --version",
    ],
))

register_preset(SandboxPreset(
    name="web-dev",
    description="Node.js with common web development tools",
    setup_commands=[
        "npm install -g typescript ts-node prettier eslint",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="python-ml",
    description="Python with PyTorch and common ML libraries",
    setup_commands=[
        "pip install torch transformers datasets accelerate",
    ],
    cpu=2.0,
    memory_mb=4096,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="empty",
    description="Bare sandbox with no additional setup",
    setup_commands=[],
))
