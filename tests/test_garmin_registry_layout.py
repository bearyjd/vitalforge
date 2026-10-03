"""The durable token layout is defined once, in `garmin_registry_common`.

The facade builds `<root>/person-<id>/generation-<n>/` and the legacy adoption
globs it from the other side of the module boundary; both read the prefixes
at call time, so changing them in one place moves both.
"""

from shared import garmin_registry, garmin_registry_common, garmin_registry_legacy


def test_the_moved_store_glob_follows_the_facade_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path)
    monkeypatch.setattr(garmin_registry_common, "PERSON_DIR_PREFIX", "member-")
    monkeypatch.setattr(garmin_registry_common, "GENERATION_DIR_PREFIX", "gen-")
    store = garmin_registry.resolve_token_dir(7, 1)
    store.mkdir(parents=True)
    (store / "garmin_tokens.json").write_text("{}", encoding="ascii")

    assert store.relative_to(tmp_path).as_posix() == "member-7/gen-1"
    assert garmin_registry_legacy._moved_store_person_ids(tmp_path) == [7]


def test_the_moved_store_scan_reads_only_ascii_decimal_person_ids(tmp_path):
    """`isdigit()` accepts `²` (which int() rejects, aborting the adoption)
    and `٣` (which int() maps to 3, misattributing the store)."""
    for name in ("person-5", "person-²", "person-٣", "person-+4"):
        store = tmp_path / name / "generation-1"
        store.mkdir(parents=True)
        (store / "garmin_tokens.json").write_text("{}", encoding="ascii")

    assert garmin_registry_legacy._moved_store_person_ids(tmp_path) == [5]
