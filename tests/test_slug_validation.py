"""Tests for shared/auth.py's slug validation helpers (spec §f.4)."""

from shared.auth import _RESERVED_SLUGS, _SLUG_RE, _slugify


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Jane Doe") == "jane-doe"


def test_slugify_strips_leading_trailing_hyphens():
    assert _slugify("  --Jane--  ") == "jane"


def test_slugify_normalizes_unicode():
    assert _slugify("José") == "jose"


def test_slugify_truncates_to_32_chars():
    raw = "a" * 50
    result = _slugify(raw)
    assert len(result) <= 32
    assert _SLUG_RE.match(result)


def test_slugify_returns_empty_string_for_unusable_input():
    assert _slugify("!!!") == ""
    assert _slugify("") == ""
    assert _slugify("---") == ""


def test_slug_regex_rejects_uppercase_and_slashes():
    assert not _SLUG_RE.match("Jane")
    assert not _SLUG_RE.match("a/b")
    assert not _SLUG_RE.match("")


def test_reserved_slugs_cover_real_path_segments():
    for segment in ("api", "auth", "static", "health", "admin"):
        assert segment in _RESERVED_SLUGS
