"""Tests for the stdout → progress-dict parser (pure, no subprocess/torch).

Line formats pinned in research-gpt-sovits.md §5 (s2/SoVITS training stdout)
and the checkpoint filename pattern in §2 (s1/GPT training — no parseable
stdout, so progress there is inferred from checkpoint files instead).
"""

from __future__ import annotations

import progress


# ---------------------------------------------------------------------------
# ETA helper (same linear-extrapolation convention as the xtts template's
# engine.compute_eta_secs — reused here since the tracker itself has no
# wall-clock access; the subprocess driver supplies elapsed time).
# ---------------------------------------------------------------------------


class TestComputeEta:
    def test_zero_before_first_step(self):
        assert progress.compute_eta_secs(10.0, 0, 100) == 0.0

    def test_zero_at_completion(self):
        assert progress.compute_eta_secs(100.0, 100, 100) == 0.0

    def test_linear_extrapolation(self):
        assert progress.compute_eta_secs(10.0, 100, 400) == 30.0

    def test_guards_zero_total(self):
        assert progress.compute_eta_secs(10.0, 5, 0) == 0.0


# ---------------------------------------------------------------------------
# Individual line parsers
# ---------------------------------------------------------------------------


class TestParseEpochPercentLine:
    def test_matches(self):
        assert progress.parse_epoch_percent_line(
            "INFO:myvoice:Train Epoch: 3 [45%]"
        ) == (3, 45)

    def test_zero_percent(self):
        assert progress.parse_epoch_percent_line(
            "INFO:myvoice:Train Epoch: 1 [0%]"
        ) == (1, 0)

    def test_non_matching_line_returns_none(self):
        assert progress.parse_epoch_percent_line("some unrelated line") is None


class TestParseLossLine:
    def test_matches_list_repr(self):
        line = "INFO:myvoice:[2.31, 4.02, 1.15, 12.4, 0.08, 0.9, 1284, 9.98e-05]"
        result = progress.parse_loss_line(line)
        assert result == {
            "loss_disc": 2.31,
            "loss_gen": 4.02,
            "loss_fm": 1.15,
            "loss_mel": 12.4,
            "kl_ssl": 0.08,
            "loss_kl": 0.9,
            "global_step": 1284,
            "lr": 9.98e-05,
        }

    def test_non_matching_line_returns_none(self):
        assert progress.parse_loss_line("INFO:myvoice:Train Epoch: 3 [45%]") is None

    def test_junk_line_returns_none(self):
        assert progress.parse_loss_line("just some noise") is None


class TestParseEpochBoundaryLine:
    def test_matches(self):
        assert progress.parse_epoch_boundary_line("====> Epoch: 3") == 3

    def test_non_matching_line_returns_none(self):
        assert progress.parse_epoch_boundary_line("Epoch: 3") is None


class TestParseCkptSaveLine:
    def test_matches_success(self):
        line = "saving ckpt myvoice_e10:Success."
        assert progress.parse_ckpt_save_line(line) == {"name": "myvoice", "epoch": 10}

    def test_non_matching_line_returns_none(self):
        assert progress.parse_ckpt_save_line("saving something else") is None


class TestSentinels:
    def test_start_sentinel_matches(self):
        assert progress.parse_start_sentinel("start training from epoch 0") == 0

    def test_start_sentinel_non_matching(self):
        assert progress.parse_start_sentinel("training done") is None

    def test_is_training_done(self):
        assert progress.is_training_done_line("training done") is True

    def test_is_training_done_false_for_other_lines(self):
        assert progress.is_training_done_line("still going") is False


# ---------------------------------------------------------------------------
# Stateful s2 (SoVITS) tracker
# ---------------------------------------------------------------------------


class TestSovitsProgressTracker:
    def test_initial_state(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        state = tracker.state()
        assert state["phase"] == "training_sovits"
        assert state["total_epochs"] == 10
        assert state["epoch"] == 0
        assert state["train_loss"] is None

    def test_epoch_percent_line_updates_epoch_and_step(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        state = tracker.feed("INFO:myvoice:Train Epoch: 3 [45%]")
        assert state["epoch"] == 3
        assert state["step"] == 45
        assert state["total_steps"] == 100

    def test_loss_line_updates_train_loss_from_loss_gen(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        tracker.feed("INFO:myvoice:Train Epoch: 3 [45%]")
        state = tracker.feed(
            "INFO:myvoice:[2.31, 4.02, 1.15, 12.4, 0.08, 0.9, 1284, 9.98e-05]"
        )
        # loss_gen (second element) is the reported train_loss.
        assert state["train_loss"] == 4.02

    def test_epoch_boundary_marks_epoch_fully_complete(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        tracker.feed("INFO:myvoice:Train Epoch: 3 [90%]")
        state = tracker.feed("====> Epoch: 3")
        assert state["epoch"] == 3
        assert state["step"] == 100
        assert state["total_steps"] == 100

    def test_start_sentinel_sets_epoch(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        state = tracker.feed("start training from epoch 2")
        assert state["epoch"] == 2

    def test_training_done_completes_progress(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        state = tracker.feed("training done")
        assert state["epoch"] == 10
        assert state["step"] == 100
        assert state["total_steps"] == 100

    def test_ckpt_save_line_does_not_change_progress_fields(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        before = tracker.feed("INFO:myvoice:Train Epoch: 3 [45%]")
        after = tracker.feed("saving ckpt myvoice_e3:Success.")
        assert after["epoch"] == before["epoch"]
        assert after["step"] == before["step"]

    def test_junk_lines_are_ignored(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        tracker.feed("INFO:myvoice:Train Epoch: 3 [45%]")
        state_before = dict(tracker.state())
        state_after = tracker.feed("some completely unrelated log line")
        assert state_after == state_before

    def test_phase_is_always_training_sovits(self):
        tracker = progress.SovitsProgressTracker(total_epochs=10)
        state = tracker.feed("INFO:myvoice:Train Epoch: 3 [45%]")
        assert state["phase"] == "training_sovits"


# ---------------------------------------------------------------------------
# s1 (GPT) checkpoint-epoch helper — no parseable stdout, infer from files.
# ---------------------------------------------------------------------------


class TestGptCheckpointEpoch:
    def test_picks_highest_epoch(self):
        files = ["myvoice-e1.ckpt", "myvoice-e5.ckpt", "myvoice-e3.ckpt"]
        assert progress.latest_gpt_checkpoint_epoch(files, total_epochs=20) == 5

    def test_ignores_non_matching_files(self):
        files = ["myvoice-e2.ckpt", "readme.txt", "other-model.ckpt"]
        assert progress.latest_gpt_checkpoint_epoch(files, total_epochs=20) == 2

    def test_no_matching_files_returns_none(self):
        assert progress.latest_gpt_checkpoint_epoch([], total_epochs=20) is None
        assert progress.latest_gpt_checkpoint_epoch(["junk.txt"], total_epochs=20) is None

    def test_clamped_to_total_epochs(self):
        # Stale checkpoint files from a previous, longer run shouldn't report
        # an epoch beyond what this run was configured for.
        files = ["myvoice-e99.ckpt"]
        assert progress.latest_gpt_checkpoint_epoch(files, total_epochs=20) == 20

    def test_different_exp_names_all_considered(self):
        files = ["alpha-e1.ckpt", "beta-e7.ckpt"]
        assert progress.latest_gpt_checkpoint_epoch(files, total_epochs=20) == 7
