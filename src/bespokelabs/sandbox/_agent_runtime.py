from __future__ import annotations

import shlex

from bespokelabs.sandbox.backends._prelude import (
    PYTHON_PREAMBLE,
    SHELL_PRELUDE,
    is_python_language,
    rewrite_redirects,
)

SHELL_COMMANDS = {"bash", "sh", "zsh"}


def normalize_sandbox_path(path: str) -> str:
    """Return an absolute sandbox path.

    File helpers treat relative paths as relative to the sandbox root on
    local-style backends. Agent launch should pass that same path to the
    command even after changing cwd.
    """
    if path.startswith("/"):
        return path
    return "/" + path


def prepare_inside_command(command: list[str]) -> list[str]:
    """Prepare an inside-agent command without mutating the user's spec.

    Local-style backends need the same Python and shell path rebasing used by
    execute_code()/execute_command(). On container/cloud backends SANDBOX_ROOT
    is unset, so the injected Python preamble and shell path expansion no-op.
    """
    command = list(command)
    if len(command) < 3:
        return command

    executable = command[0].rsplit("/", 1)[-1]
    if executable in SHELL_COMMANDS:
        return _prepare_inline_shell_command(command)
    if not is_python_language(executable):
        return command

    try:
        code_index = command.index("-c") + 1
    except ValueError:
        return command
    if code_index >= len(command):
        return command

    command[code_index] = PYTHON_PREAMBLE + command[code_index]
    return command


def build_inside_shell_script(
    *,
    command: list[str],
    input_mode: str,
    prompt: str,
    cwd: str | None,
    env: dict[str, str],
    input_path: str,
) -> str:
    command = prepare_inside_command(command)
    lines = ["set -e"]

    for key, value in env.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    if cwd:
        lines.append(f"cd {shell_path(cwd)}")

    command_line = " ".join(shell_arg(part, is_command=(idx == 0)) for idx, part in enumerate(command))
    if input_mode == "stdin":
        lines.append(f"printf %s {shlex.quote(prompt)} | {command_line}")
    elif input_mode == "argv":
        lines.append(f"{command_line} {shlex.quote(prompt)}")
    elif input_mode == "file":
        lines.append(f"{command_line} {shell_path(input_path)}")
    elif input_mode == "none":
        lines.append(command_line)
    else:
        raise ValueError(f"Unsupported inside agent input_mode: {input_mode}")
    return "\n".join(lines)


def build_patch_apply_command(*, patch_path: str, strip: int) -> str:
    patch_arg = shlex.quote(f"-p{strip}")
    patch_file = shell_path(patch_path)
    return f"patch {patch_arg} < {patch_file}"


def shell_arg(value: str, *, is_command: bool = False) -> str:
    if value.startswith("/") and not is_command:
        return shell_path(value)
    return shlex.quote(value)


def shell_path(path: str) -> str:
    if not path.startswith("/"):
        return shlex.quote(path)
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"${{SANDBOX_ROOT:-}}{escaped}"'


def _prepare_inline_shell_command(command: list[str]) -> list[str]:
    command = list(command)
    code_index = _shell_code_index(command)
    if code_index is None:
        return command
    command[code_index] = SHELL_PRELUDE + rewrite_redirects(command[code_index])
    return command


def _shell_code_index(command: list[str]) -> int | None:
    for index, arg in enumerate(command[1:], start=1):
        if not (arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])):
            continue
        code_index = index + 1
        if code_index < len(command):
            return code_index
        return None
    return None

