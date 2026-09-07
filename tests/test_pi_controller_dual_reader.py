"""Unit tests for pi_controller/dual_reader.py's transport-selection logic --
USB vs. network, picked by mocking each module's is_device_present()/
read_tags() rather than touching real hardware or a socket."""

import pytest

from pi_controller import dual_reader


class _StopTest(Exception):
    """Raised from a mocked read_tags() to unwind dual_reader.read_tags()'s
    otherwise-infinite loop once the test has seen what it needs to see."""


def test_prefers_usb_when_both_transports_are_present(monkeypatch):
    calls = []
    monkeypatch.setattr(dual_reader.hid_tag_reader, "is_device_present", lambda: True)
    monkeypatch.setattr(dual_reader.net_tag_reader, "is_device_present", lambda: True)

    def fake_hid_read_tags(on_tag):
        calls.append("hid")
        raise _StopTest

    def fake_net_read_tags(on_tag):
        calls.append("net")
        raise _StopTest

    monkeypatch.setattr(dual_reader.hid_tag_reader, "read_tags", fake_hid_read_tags)
    monkeypatch.setattr(dual_reader.net_tag_reader, "read_tags", fake_net_read_tags)

    with pytest.raises(_StopTest):
        dual_reader.read_tags(lambda tag_id: None)

    assert calls == ["hid"]


def test_falls_back_to_network_when_usb_is_not_present(monkeypatch):
    calls = []
    monkeypatch.setattr(dual_reader.hid_tag_reader, "is_device_present", lambda: False)
    monkeypatch.setattr(dual_reader.net_tag_reader, "is_device_present", lambda: True)

    def fake_net_read_tags(on_tag):
        calls.append("net")
        raise _StopTest

    monkeypatch.setattr(dual_reader.net_tag_reader, "read_tags", fake_net_read_tags)

    with pytest.raises(_StopTest):
        dual_reader.read_tags(lambda tag_id: None)

    assert calls == ["net"]


def test_reprobes_when_neither_transport_is_present_yet(monkeypatch):
    probe_calls = {"count": 0}
    monkeypatch.setattr(dual_reader, "_PROBE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(dual_reader.time, "sleep", lambda seconds: None)

    def hid_present():
        probe_calls["count"] += 1
        if probe_calls["count"] >= 3:
            raise _StopTest
        return False

    monkeypatch.setattr(dual_reader.hid_tag_reader, "is_device_present", hid_present)
    monkeypatch.setattr(dual_reader.net_tag_reader, "is_device_present", lambda: False)

    with pytest.raises(_StopTest):
        dual_reader.read_tags(lambda tag_id: None)

    assert probe_calls["count"] == 3


def test_reprobes_after_a_delegated_read_tags_returns_unexpectedly(monkeypatch):
    # hid_tag_reader.read_tags() never returns under normal operation, but if
    # it somehow did (a bug, or resilience logic giving up), dual_reader
    # should treat that as "start over from probing," not silently stop.
    calls = []
    monkeypatch.setattr(dual_reader.hid_tag_reader, "is_device_present", lambda: True)
    monkeypatch.setattr(dual_reader.net_tag_reader, "is_device_present", lambda: False)

    def fake_hid_read_tags(on_tag):
        calls.append("hid")
        if len(calls) >= 2:
            raise _StopTest
        return  # returns instead of raising, simulating an unexpected exit

    monkeypatch.setattr(dual_reader.hid_tag_reader, "read_tags", fake_hid_read_tags)

    with pytest.raises(_StopTest):
        dual_reader.read_tags(lambda tag_id: None)

    assert calls == ["hid", "hid"]
