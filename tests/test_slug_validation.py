"""Tests for shared/slugs.py (spec §f.4)."""

from shared.slugs import RESERVED_SLUGS, SLUG_RE, slugify


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Jane Doe") == "jane-doe"


def test_slugify_strips_leading_trailing_hyphens():
    assert slugify("  --Jane--  ") == "jane"


def test_slugify_normalizes_unicode():
    assert slugify("José") == "jose"


def test_slugify_truncates_to_32_chars():
    raw = "a" * 50
    result = slugify(raw)
    assert len(result) <= 32
    assert SLUG_RE.match(result)


def test_slugify_returns_empty_string_for_unusable_input():
    assert slugify("!!!") == ""
    assert slugify("") == ""
    assert slugify("---") == ""


def test_slug_regex_rejects_uppercase_and_slashes():
    assert not SLUG_RE.match("Jane")
    assert not SLUG_RE.match("a/b")
    assert not SLUG_RE.match("")


def test_reserved_slugs_cover_real_path_segments():
    for segment in ("api", "auth", "static", "health", "admin"):
        assert segment in RESERVED_SLUGS
