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

# -- Built-in presets ------------------------------------------------------

register_preset(SandboxPreset(
    name="claude-code",
    description="Sandbox with Claude Code (Anthropic CLI) installed",
    image=f"{IMAGE_REGISTRY}/claude-code:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "npm install -g @anthropic-ai/claude-code",
    ],
    memory_mb=2048,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="claude-sdk",
    description="Sandbox with Claude Agent SDK and bundled Claude Code CLI",
    image=f"{IMAGE_REGISTRY}/claude-sdk:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install claude-agent-sdk",
    ],
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
    memory_mb=2048,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="python-data-science",
    description="Python with numpy, pandas, matplotlib, scikit-learn",
    image=f"{IMAGE_REGISTRY}/python-data-science:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install numpy pandas matplotlib scikit-learn",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="node",
    description="Node.js 20 LTS with TypeScript toolchain (typescript, ts-node, pnpm, yarn)",
    image=f"{IMAGE_REGISTRY}/node:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "npm install -g typescript ts-node",
        "corepack enable",
        "corepack prepare pnpm@latest yarn@stable --activate",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="web-dev",
    description="Node.js with common web development tools",
    image=f"{IMAGE_REGISTRY}/web-dev:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "npm install -g typescript ts-node prettier eslint",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="python-ml",
    description="Python with PyTorch and common ML libraries",
    image=f"{IMAGE_REGISTRY}/python-ml:{PRESET_IMAGE_TAG}",
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

# -- Language runtimes -----------------------------------------------------

register_preset(SandboxPreset(
    name="go",
    description="Go 1.22 toolchain",
    image=f"{IMAGE_REGISTRY}/go:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "go version",
    ],
    env_vars={"GOFLAGS": "-buildvcs=false"},
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="rust",
    description="Rust toolchain with clippy and rustfmt",
    image=f"{IMAGE_REGISTRY}/rust:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "rustup component add clippy rustfmt",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="java",
    description="Java 21 (Temurin JDK) with Maven and Gradle",
    image=f"{IMAGE_REGISTRY}/java:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "java -version && mvn -version && gradle -version",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="ruby",
    description="Ruby 3.3 with bundler and rake",
    image=f"{IMAGE_REGISTRY}/ruby:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "gem install bundler rake",
    ],
    memory_mb=1024,
))

register_preset(SandboxPreset(
    name="php",
    description="PHP 8.3 CLI with Composer",
    image=f"{IMAGE_REGISTRY}/php:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "php --version && composer --version",
    ],
    memory_mb=1024,
))

register_preset(SandboxPreset(
    name="dotnet",
    description=".NET 8.0 SDK",
    image=f"{IMAGE_REGISTRY}/dotnet:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "dotnet --version",
    ],
    env_vars={"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="cpp",
    description="C/C++ toolchain (gcc 13) with cmake, ninja, clang and gdb",
    image=f"{IMAGE_REGISTRY}/cpp:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "gcc --version && cmake --version",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="r",
    description="R 4.4.1 with data.table, ggplot2, dplyr and jsonlite",
    image=f"{IMAGE_REGISTRY}/r:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "Rscript -e \"install.packages(c('data.table','ggplot2','dplyr','jsonlite'), repos='https://cloud.r-project.org')\"",
    ],
    memory_mb=2048,
))

# -- ML / AI ---------------------------------------------------------------

register_preset(SandboxPreset(
    name="pytorch",
    description="PyTorch (CPU) with torchvision and torchaudio",
    image=f"{IMAGE_REGISTRY}/pytorch:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision torchaudio",
    ],
    cpu=2.0,
    memory_mb=4096,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="tensorflow",
    description="TensorFlow (CPU build)",
    image=f"{IMAGE_REGISTRY}/tensorflow:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install tensorflow-cpu",
    ],
    cpu=2.0,
    memory_mb=4096,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="huggingface",
    description="Hugging Face stack (transformers, datasets, accelerate, hub, safetensors)",
    image=f"{IMAGE_REGISTRY}/huggingface:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install transformers datasets tokenizers accelerate huggingface-hub safetensors sentencepiece",
    ],
    env_vars={"HF_HUB_DISABLE_TELEMETRY": "1"},
    cpu=2.0,
    memory_mb=4096,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="nlp",
    description="NLP toolkit (spaCy + NLTK) with en_core_web_sm and common corpora",
    image=f"{IMAGE_REGISTRY}/nlp:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install spacy nltk",
        "python -m spacy download en_core_web_sm",
        "python -m nltk.downloader -d /usr/share/nltk_data punkt stopwords wordnet",
    ],
    env_vars={"NLTK_DATA": "/usr/share/nltk_data"},
    memory_mb=2048,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="llm",
    description="LLM application stack (openai, anthropic, langchain, tiktoken, tenacity)",
    image=f"{IMAGE_REGISTRY}/llm:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install openai anthropic langchain langchain-core langchain-community tiktoken tenacity",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="scientific",
    description="Scientific computing stack (numpy, scipy, sympy, networkx, statsmodels)",
    image=f"{IMAGE_REGISTRY}/scientific:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install numpy scipy sympy networkx statsmodels",
    ],
    memory_mb=2048,
))

# -- Web / scraping / browser ---------------------------------------------

register_preset(SandboxPreset(
    name="scraping",
    description="Web scraping stack (requests, httpx, beautifulsoup4, lxml, parsel, scrapy)",
    image=f"{IMAGE_REGISTRY}/scraping:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install requests httpx beautifulsoup4 lxml parsel scrapy",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="playwright",
    description="Playwright for Python with browsers pre-installed",
    image=f"{IMAGE_REGISTRY}/playwright:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install playwright",
        "playwright install --with-deps chromium",
    ],
    cpu=2.0,
    memory_mb=4096,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="selenium",
    description="Selenium with headless Chromium and chromedriver",
    image=f"{IMAGE_REGISTRY}/selenium:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install selenium webdriver-manager",
    ],
    env_vars={"CHROME_BIN": "/usr/bin/chromium", "CHROMEDRIVER": "/usr/bin/chromedriver"},
    cpu=2.0,
    memory_mb=2048,
    timeout_secs=1800,
))

register_preset(SandboxPreset(
    name="fastapi",
    description="FastAPI web framework with Uvicorn, Pydantic and httpx",
    image=f"{IMAGE_REGISTRY}/fastapi:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install fastapi 'uvicorn[standard]' pydantic httpx",
    ],
    memory_mb=2048,
))

# -- Data / documents / media ---------------------------------------------

register_preset(SandboxPreset(
    name="dataeng",
    description="Data engineering stack (polars, duckdb, pyarrow, sqlalchemy)",
    image=f"{IMAGE_REGISTRY}/dataeng:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install polars duckdb pyarrow sqlalchemy",
    ],
    memory_mb=4096,
))

register_preset(SandboxPreset(
    name="pdf",
    description="PDF processing stack (pypdf, pdfplumber, reportlab, pdf2image) with poppler",
    image=f"{IMAGE_REGISTRY}/pdf:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install pypdf pdfplumber reportlab pdf2image",
    ],
    memory_mb=2048,
))

register_preset(SandboxPreset(
    name="image",
    description="Image processing stack (pillow, opencv-python-headless, scikit-image, numpy)",
    image=f"{IMAGE_REGISTRY}/image:{PRESET_IMAGE_TAG}",
    setup_commands=[
        "pip install pillow opencv-python-headless scikit-image numpy",
    ],
    memory_mb=2048,
))
