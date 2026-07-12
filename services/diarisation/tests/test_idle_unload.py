"""Tests for the idle-timer VRAM unloader decision logic.

The unloader is a pure state machine with an injectable clock so timing is
deterministic — no real sleeps, no asyncio, no torch required.
"""

import idle_unload


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def make(idle_secs=60.0, loaded=True, clock=None):
    """Build an IdleUnloader with a settable loaded flag and unload spy."""
    state = {"loaded": loaded, "unload_calls": 0}

    def is_loaded():
        return state["loaded"]

    def unload():
        state["unload_calls"] += 1
        state["loaded"] = False

    clock = clock or Clock()
    u = idle_unload.IdleUnloader(
        idle_secs=idle_secs, is_loaded=is_loaded, unload=unload, now=clock
    )
    return u, state, clock


def test_unloads_when_idle_loaded_and_quiet():
    u, state, clock = make(idle_secs=60.0)
    clock.advance(61)
    assert u.should_unload() is True
    assert u.perform_unload() is True
    assert state["unload_calls"] == 1
    assert state["loaded"] is False


def test_does_not_unload_while_job_active():
    u, state, clock = make(idle_secs=60.0)
    u.on_submit()
    clock.advance(120)
    assert u.should_unload() is False
    assert u.perform_unload() is False
    assert state["unload_calls"] == 0


def test_does_not_unload_before_idle_elapsed():
    u, state, clock = make(idle_secs=60.0)
    clock.advance(30)
    assert u.should_unload() is False
    assert state["unload_calls"] == 0


def test_does_not_unload_when_not_loaded():
    u, state, clock = make(idle_secs=60.0, loaded=False)
    clock.advance(120)
    assert u.should_unload() is False
    assert u.perform_unload() is False
    assert state["unload_calls"] == 0


def test_idle_timer_resets_after_each_job():
    u, state, clock = make(idle_secs=60.0)
    # A job runs and finishes at t+50
    u.on_submit()
    clock.advance(50)
    u.on_finish()
    # 40s after the job finished is still under the 60s idle window
    clock.advance(40)
    assert u.should_unload() is False
    # ...but 61s after the finish crosses it
    clock.advance(21)
    assert u.should_unload() is True


def test_perform_unload_skips_if_job_arrived_after_check():
    """Race: should_unload() saw the coast clear, but a job was submitted
    before perform_unload() runs on the executor. It must not pull the model."""
    u, state, clock = make(idle_secs=60.0)
    clock.advance(61)
    assert u.should_unload() is True
    u.on_submit()  # a job sneaks in during the gap
    assert u.perform_unload() is False
    assert state["unload_calls"] == 0


def test_perform_unload_noop_when_already_unloaded():
    u, state, clock = make(idle_secs=60.0)
    clock.advance(61)
    assert u.perform_unload() is True
    # Second pass: model is gone, nothing to do.
    assert u.should_unload() is False
    assert u.perform_unload() is False
    assert state["unload_calls"] == 1


def test_parse_idle_secs_defaults_when_absent_or_invalid():
    assert idle_unload.parse_idle_secs(None) == 60.0
    assert idle_unload.parse_idle_secs("") == 60.0
    assert idle_unload.parse_idle_secs("   ") == 60.0
    assert idle_unload.parse_idle_secs("abc") == 60.0


def test_parse_idle_secs_accepts_numbers_including_disable():
    assert idle_unload.parse_idle_secs("90") == 90.0
    assert idle_unload.parse_idle_secs("0") == 0.0  # 0 disables (caller gate)
    assert idle_unload.parse_idle_secs("-1") == -1.0
