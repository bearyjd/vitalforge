"""Import-time checks for path-valued environment variables."""


def refuse_leading_tilde(name: str, value: str) -> str:
    """Return ``value`` unchanged unless it starts with ``~``, which is refused.

    A ``~`` is never expanded by ``Path``, so it would create a literal
    ``./~/`` directory; expanding it would put the file under HOME, which in
    the images is /app, the ephemeral container layer.  A loud failure at
    import is better than either.  The message names the variable, never the
    value, so it is safe to log.
    """
    if value.startswith("~"):
        raise ValueError(f"{name} must not start with '~'; set it to an absolute path on the data volume")
    return value
