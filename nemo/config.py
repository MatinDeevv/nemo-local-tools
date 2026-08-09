from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


PROJECTS_ROOT = Path(os.getenv("NEMO_PROJECTS_ROOT", r"C:\Users\marti\Documents\NemoProjects"))
RUNTIME_ROOT = Path(os.getenv("NEMO_RUNTIME_ROOT", r"C:\Users\marti\Documents\NemoRuntime"))
DEFAULT_ENV = Path(os.getenv("NEMO_ENV_FILE", r"C:\Users\marti\Desktop\.env"))
KNOWN_OPENCODE = Path(
    os.getenv(
        "OPENCODE_BIN",
        r"C:\Users\marti\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe",
    )
)


@dataclass(frozen=True, slots=True)
class Engineer:
    id: str
    number: int
    title: str
    key_env: str
    model: str
    color: str
    focus: str

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("key_env", None)
        return value


# Model identifiers include the OpenCode provider prefix (nvidia/) followed by
# NVIDIA's catalog model id.  Each process receives exactly one API key.
TEAM: tuple[Engineer, ...] = (
    Engineer(
        "manager",
        1,
        "Manager",
        "NVIDIA_API_KEY_3",
        "nvidia/openai/gpt-oss-120b",
        "cyan",
        "Start with a timeboxed heartbeat, then integrate, test, and repair the complete product.",
    ),
    Engineer(
        "swe1",
        2,
        "SWE 1",
        "NVIDIA_API_KEY_1",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
        "green",
        "Own the core domain model, hardest algorithms, and primary computational implementation.",
    ),
    Engineer(
        "swe2",
        3,
        "SWE 2",
        "NVIDIA_API_KEY_2",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
        "magenta",
        "Own complementary algorithms, numerical methods, data structures, and performance-sensitive code.",
    ),
    Engineer(
        "swe3",
        4,
        "SWE 3",
        "NVIDIA_API_KEY_5",
        "nvidia/nvidia/nemotron-3-super-120b-a12b",
        "yellow",
        "Own package architecture, configuration, CLI, persistence, reports, and user-facing integration.",
    ),
    Engineer(
        "swe4",
        5,
        "SWE 4",
        "NVIDIA_API_KEY_4",
        "nvidia/nvidia/nemotron-3-super-120b-a12b",
        "blue",
        "Own diagnostics, robustness, parallelism, secondary computation, and cross-module glue code.",
    ),
)

# GPT-OSS is useful as the concurrent manager/coordinator but is materially
# slower per native tool call on NVIDIA Build. Integration is coding-heavy, so
# the same manager/key switches to Super after all four SWE handoffs.
INTEGRATION_MANAGER_MODEL = "nvidia/nvidia/nemotron-3-super-120b-a12b"


def safe_slug(value: str, maximum: int = 54) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return (slug or "project")[:maximum]


def load_dotenv(path: Path, *, overwrite: bool = False) -> dict[str, str]:
    """Load a small .env file without logging or returning secret values."""
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")
    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip().strip('"').strip("'")
        if overwrite or key not in os.environ:
            os.environ[key] = value
        loaded[key] = key
    return loaded


def find_opencode() -> Path:
    if KNOWN_OPENCODE.is_file():
        return KNOWN_OPENCODE.resolve()
    found = shutil.which("opencode") or shutil.which("opencode.exe")
    if not found:
        raise FileNotFoundError(
            "OpenCode was not found. Install opencode-ai or set OPENCODE_BIN to its executable."
        )
    return Path(found).resolve()


def missing_keys() -> list[str]:
    return [person.key_env for person in TEAM if not os.getenv(person.key_env)]


def find_git_bash() -> Path | None:
    """Find Git Bash so coding models can use the shell syntax they generate best."""
    configured = os.getenv("OPENCODE_GIT_BASH_PATH")
    candidates = [
        Path(configured) if configured else None,
        Path.home() / "scoop/apps/git/current/bin/bash.exe",
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def runtime_environment(person: Engineer, run_dir: Path, venv_python: Path) -> dict[str, str]:
    """Return a role-isolated, non-ChatGPT OpenCode environment."""
    env = os.environ.copy()
    env["NVIDIA_API_KEY"] = os.environ[person.key_env]
    role_root = RUNTIME_ROOT / "opencode" / run_dir.name / person.id
    env["XDG_DATA_HOME"] = str(role_root / "data")
    env["XDG_CONFIG_HOME"] = str(role_root / "config")
    env["XDG_STATE_HOME"] = str(role_root / "state")
    env["XDG_CACHE_HOME"] = str(RUNTIME_ROOT / "cache")
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    git_bash = find_git_bash()
    if git_bash:
        env["OPENCODE_GIT_BASH_PATH"] = str(git_bash)
    env["PIP_CACHE_DIR"] = str(RUNTIME_ROOT / "cache" / "pip")
    env["VIRTUAL_ENV"] = str(venv_python.parent.parent)
    env["PATH"] = str(venv_python.parent) + os.pathsep + env.get("PATH", "")
    return env
