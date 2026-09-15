"""Reading a secret from a file instead of the environment (T8, AC15-AC17).

B-11's second blocker, and the one the charter admits got *worse*: Iteration 10
added a second secret, so there are now two -- `GROQ_API_KEY` and
`QUERYPILOT_USERS` -- and both arrive the same way, interpolated from a
gitignored `.env` into a compose `environment:` block.

On a laptop that is fine. On a host it is not, and the reason is specific
rather than theoretical: an environment variable is part of the container's
configuration, so `docker inspect querypilot-api` prints both secrets in full,
as does anything else that can reach the Docker socket. It is also inherited by
every child process and visible in `/proc/1/environ`. A file is readable by
whoever the file's mode says and nobody else.

So every secret gains a `_FILE` sibling. `GROQ_API_KEY_FILE=/run/secrets/groq`
is read from that path; `GROQ_API_KEY` still works and still means what it
meant. The file form wins when both are set, because an operator who went to
the trouble of mounting a file meant it.

**A missing file fails closed, loudly in the log and silently to the caller.**
It does not fall back to the environment variable. An operator who said "the
secret is in this file" and whose file is absent has a broken deployment, and
quietly using a stale environment variable instead -- or worse, an empty
default that looks configured -- is how a deployment comes to be running on a
credential nobody remembers setting. The caller gets the default, which for
`QUERYPILOT_USERS` means the gate refuses every request, and `/health` says so.

**Nothing here ever logs a value.** The path is logged; the contents are not,
not on success and not on failure. That is the discipline
`api/http/auth.py` already holds and `tests/test_auth_logic.py:116-152` already
enforces, extended to the reader that now sits in front of it.
"""

from __future__ import annotations

import logging
import os
import pathlib

logger = logging.getLogger("querypilot")

#: The suffix that turns an environment variable into a file reference.
FILE_SUFFIX = "_FILE"


def file_variable(name: str) -> str:
    """The name of the `_FILE` sibling for a secret."""
    return f"{name}{FILE_SUFFIX}"


def read_secret_file(path: str) -> str | None:
    """Read a secret from a path, or return None having said why.

    The trailing newline is stripped, because `echo secret > file` puts one
    there and a credential with an invisible newline on the end fails in a way
    that looks like the credential being wrong.

    Only the trailing newline. Leading and interior whitespace are preserved --
    a secret is an opaque string and this is not the layer that gets to decide
    which of its bytes are decorative.
    """
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("secret file %s does not exist; treating the secret as unset", path)
        return None
    except PermissionError:
        logger.error("secret file %s is not readable; treating the secret as unset", path)
        return None
    except OSError as exc:
        # `exc` here is an OSError from the filesystem: it carries the path and
        # an errno, never the file's contents.
        logger.error("secret file %s could not be read (%s)", path, exc.strerror)
        return None
    except UnicodeDecodeError:
        # Deliberately does not include the exception, whose message quotes the
        # offending bytes -- which are the secret.
        logger.error("secret file %s is not valid UTF-8; treating the secret as unset", path)
        return None

    return text.rstrip("\n")


def get_secret(name: str, default: str | None = None) -> str | None:
    """The value of a secret, from its `_FILE` sibling if one is configured.

    Resolution order, and the reason for it:

    1. `<NAME>_FILE`, if set to a non-blank path. It wins over the environment
       variable because configuring it is a deliberate act.
    2. `<NAME>`, the ordinary environment variable, unchanged in behaviour.
    3. `default`.

    A configured-but-unreadable file returns `default` and does **not** fall
    through to step 2. See the module docstring.
    """
    path = os.environ.get(file_variable(name), "").strip()
    if path:
        value = read_secret_file(path)
        return default if value is None else value
    return os.environ.get(name, default)


def secret_status(name: str) -> dict:
    """Where a secret came from, for `/health` to report. Never its value.

    Says `file`, `environment` or `unset`, and for the file form whether the
    file was actually readable -- which is the one thing an operator cannot
    determine from outside and the failure this module is most likely to hit.
    """
    path = os.environ.get(file_variable(name), "").strip()
    if path:
        return {
            "source": "file",
            "readable": read_secret_file(path) is not None,
        }
    if os.environ.get(name, "").strip():
        return {"source": "environment", "readable": True}
    return {"source": "unset", "readable": False}
