"""Unit tests for pi_controller/gpio_relay.py's graceful degradation when no
real GPIO hardware is available (e.g. running on a Windows PC instead of the
Pi) - distinct from DRY_RUN, which is "skip real hardware on purpose."
Here the code genuinely tries to talk to GPIO hardware and that fails; it
should log clearly and no-op, never crash the caller."""

import pi_controller.gpio_relay as gpio_relay


def _reset_module_state(monkeypatch):
    monkeypatch.setattr(gpio_relay, "_relay", None)
    monkeypatch.setattr(gpio_relay, "_warned_unavailable", False)
    monkeypatch.setattr(gpio_relay.config, "DRY_RUN", False)


def test_activate_does_not_raise_when_gpiozero_is_unavailable(monkeypatch):
    _reset_module_state(monkeypatch)
    # No real gpiozero backend on this dev machine - constructing the
    # device raises, same as it would on a genuine non-Pi machine.
    gpio_relay.activate()  # must not raise
    assert gpio_relay._relay == gpio_relay._UNAVAILABLE


def test_deactivate_does_not_raise_when_gpiozero_is_unavailable(monkeypatch):
    _reset_module_state(monkeypatch)
    gpio_relay.deactivate()  # must not raise
    assert gpio_relay._relay == gpio_relay._UNAVAILABLE


def test_unavailable_warning_is_logged_only_once(monkeypatch, caplog):
    _reset_module_state(monkeypatch)
    import logging

    with caplog.at_level(logging.WARNING, logger="pi_controller.gpio_relay"):
        gpio_relay.activate()
        gpio_relay.activate()
        gpio_relay.deactivate()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_cleanup_is_safe_when_relay_was_never_available(monkeypatch):
    _reset_module_state(monkeypatch)
    gpio_relay.activate()
    assert gpio_relay._relay == gpio_relay._UNAVAILABLE
    gpio_relay.cleanup()  # must not raise (no .close() to call on a string sentinel)
    assert gpio_relay._relay is None


def test_dry_run_still_takes_priority_over_the_unavailable_path(monkeypatch):
    monkeypatch.setattr(gpio_relay, "_relay", None)
    monkeypatch.setattr(gpio_relay, "_warned_unavailable", False)
    monkeypatch.setattr(gpio_relay.config, "DRY_RUN", True)

    gpio_relay.activate()
    assert gpio_relay._relay == "dry-run"
