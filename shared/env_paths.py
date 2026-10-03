"""Import-time checks for path-valued environment variables."""

import os


def refuse_leading_tilde(name: str, value: str) -> str:
    """Return ``value`` unchanged unless it starts with ``~``, which is refused.

    A ``~`` is never expanded by ``Path``, so it would create a literal
    ``./~/`` directory; expanding it would put the file under HOME, which in
    the images is /app, the ephemeral container layer.  A loud failure at
    import is better than either.  The message names the variable, never the
    value, so it is safe to log.
    """
    if value.startswith("~"):
        raise ValueError(f"{name} must not start with '~' (it is never expanded); write the path out in full")
    return value


def path_from_env(name: str, default: str) -> str:
    """The variable's value, with a blank or whitespace-only one (compose's
    ``${NAME:-}``) treated as unset; a leading ``~`` is refused."""
    value = os.getenv(name, "")
    return refuse_leading_tilde(name, value if value.strip() else default)
