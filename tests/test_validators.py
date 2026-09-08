"""utils/validators.py - small pure-function helpers, unit-tested directly
(no DB/app context needed for these two)."""

from utils.validators import escape_like_wildcards, decimal_scan_hex_suffix


def test_escape_like_wildcards_escapes_percent_and_underscore():
    assert escape_like_wildcards("50%") == "50\\%"
    assert escape_like_wildcards("a_b") == "a\\_b"
    assert escape_like_wildcards("plain text") == "plain text"


def test_escape_like_wildcards_escapes_literal_backslash_too():
    # Must escape backslash FIRST, or an already-escaped \% could be
    # re-interpreted - covers that ordering bug class.
    assert escape_like_wildcards("a\\b") == "a\\\\b"
    assert escape_like_wildcards("100\\%") == "100\\\\\\%"


def test_decimal_scan_hex_suffix_still_works():
    # Unrelated helper in the same module - just confirms the file still
    # imports/parses cleanly alongside the new function.
    assert decimal_scan_hex_suffix("14754629") == "E12345"
    assert decimal_scan_hex_suffix("not-a-number") is None
