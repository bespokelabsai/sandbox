"""Shared sandbox preludes for the local and Ray backends.

The shell prelude, Python preamble, and redirect-rewriting logic are
used identically by both backends.  Keeping them in one module avoids
silent divergence.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Shell prelude
# ---------------------------------------------------------------------------
# Injected into ``bash -c`` commands.  Defines wrapper functions for common
# file utilities so that absolute-path arguments are transparently rewritten
# to ``$SANDBOX_ROOT/...`` before the real binary runs.

SHELL_PRELUDE = r"""
__sb_run() {
    local cmd="$1"; shift
    local args=()
    for arg in "$@"; do
        if [[ "$arg" == /* ]]; then
            args+=("${SANDBOX_ROOT}${arg}")
        else
            args+=("$arg")
        fi
    done
    command "$cmd" "${args[@]}"
}
for __c in cat ls cp mv head tail wc grep find rm mkdir touch chmod stat file; do
    eval "$__c() { __sb_run $__c \"\$@\"; }"
done
"""

# ---------------------------------------------------------------------------
# Python preamble
# ---------------------------------------------------------------------------
# Injected into ``execute_code()`` invocations.  Monkey-patches
# ``builtins.open``, ``io.open``, and common ``os`` functions so that
# absolute paths resolve under ``$SANDBOX_ROOT`` instead of the host root.
#
# * Paths already inside the sandbox are detected by prefix and left alone
#   to prevent double-rebasing (e.g. ``os.makedirs`` calling ``os.mkdir``).
# * ``/dev/``, ``/proc/``, ``/sys/`` are exempt so special files keep working.
# * Dual-path functions (``os.rename``, ``os.replace``) rebase both arguments.

PYTHON_PREAMBLE = """\
def _sb_setup():
    import builtins, io, os
    root = os.environ.get("SANDBOX_ROOT", "")
    if not root:
        return
    pfx = root + "/"
    def rp(p):
        if isinstance(p, str) and p.startswith("/") and not (
            p.startswith((pfx, "/dev/", "/proc/", "/sys/")) or p == root
        ):
            return root + p
        if hasattr(p, "__fspath__"):
            s = os.fspath(p)
            if isinstance(s, str) and s.startswith("/") and not (
                s.startswith((pfx, "/dev/", "/proc/", "/sys/")) or s == root
            ):
                import pathlib
                return pathlib.Path(root + s)
        return p
    _orig = builtins.open
    def _open(f, *a, **k):
        return _orig(rp(f), *a, **k)
    builtins.open = io.open = _open
    for _n in ("stat", "lstat", "listdir", "scandir", "mkdir", "makedirs",
               "remove", "unlink", "rmdir", "open", "chmod"):
        _f = getattr(os, _n, None)
        if _f:
            def _w(_o=_f):
                def _fn(p, *a, **k):
                    return _o(rp(p), *a, **k)
                return _fn
            setattr(os, _n, _w())
    for _n in ("rename", "replace"):
        _f = getattr(os, _n, None)
        if _f:
            def _w2(_o=_f):
                def _fn(src, dst, *a, **k):
                    return _o(rp(src), rp(dst), *a, **k)
                return _fn
            setattr(os, _n, _w2())
_sb_setup()
del _sb_setup
"""

# ---------------------------------------------------------------------------
# Redirect rewriting
# ---------------------------------------------------------------------------
# Rewrites absolute paths that appear after shell redirect operators
# (``>``, ``>>``, ``<``) so they point into ``$SANDBOX_ROOT``.
#
# The regex uses alternation: quoted strings (single or double) are matched
# first and returned unchanged, so paths inside quotes are never rewritten.
# ``(?<!<)`` prevents matching the ``<`` inside here-docs/here-strings
# (``<<``, ``<<<``).

_REDIRECT_RE = re.compile(
    r"""("(?:[^"\\]|\\.)*"|'[^']*')"""    # group 1: quoted string (skip)
    r"""|(>[>]?\s*|(?<!<)<\s*)"""          # group 2: redirect operator
    r"""(/[^\s])""",                       # group 3: start of absolute path
    re.DOTALL,
)

# Pre-compiled pattern for identifying Python interpreter names.
# Accepts version suffixes like "t" (free-threaded) or "d" (debug),
# e.g. python3.13t, python3.13d.
_PYTHON_LANG_RE = re.compile(r"python\d+(\.\d+)*[a-z]*$|python$")


def rewrite_redirects(command: str) -> str:
    """Rewrite absolute paths in shell redirections (``>``, ``>>``, ``<``).

    Quoted strings are left intact to avoid false positives such as
    ``echo ">/tmp/not-a-redirect"``.
    """
    def _repl(m: re.Match) -> str:
        if m.group(1):  # quoted string — pass through unchanged
            return m.group(0)
        return m.group(2) + "${SANDBOX_ROOT}" + m.group(3)

    return _REDIRECT_RE.sub(_repl, command)


def is_python_language(language: str) -> bool:
    """Return True if *language* names a Python interpreter."""
    return bool(_PYTHON_LANG_RE.match(language))
