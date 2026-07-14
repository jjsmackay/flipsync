"""Global GPU semaphore tests.

GPU-bound jobs (vocal_separation, diarisation, transcription_bulk,
transcription_segment) serialise host-wide: at most one GPU job runs across
ALL projects at any time. CPU jobs (extract_audio, export) are not gated.
Per-project FIFO ordering is unchanged.

Handlers are mocked via monkeypatch on jobs.HANDLERS so no ffmpeg or external
service is needed.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_project():
    """Bootstrap a project DB directly without going through the HTTP layer.

    Relies on the autouse isolated_data_dir fixture for DATA_DIR isolation.
    """
    import db

    project_id = str(uuid.uuid4())
    db.create_project_db(project_id)
    conn = db.get_conn(project_id)
    now = _now()
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at, status) VALUES (?,?,?,?,'new')",
        (project_id, "Test", now, now),
    )
    conn.commit()
    return project_id


class TestGpuJobTypes:
    def test_gpu_job_types_registry(self):
        import jobs

        assert jobs.GPU_JOB_TYPES == {
            "vocal_separation",
            "diarisation",
            "scout_speakers",
            "transcription_bulk",
            "transcription_segment",
            "reference_transcribe",
            "finetune",
            "preview",
        }
        # Reference transcription gates on the transcription service.
        assert jobs.GPU_JOB_SERVICES["reference_transcribe"] == "transcription"
        # CPU jobs must never be gated (dataset_build uses the CPU-only
        # cleanup service).
        assert "extract_audio" not in jobs.GPU_JOB_TYPES
        assert "export" not in jobs.GPU_JOB_TYPES
        assert "dataset_build" not in jobs.GPU_JOB_TYPES
        # XTTS jobs gate on the xtts service (readiness wait target).
        assert jobs.GPU_JOB_SERVICES["finetune"] == "xtts"
        assert jobs.GPU_JOB_SERVICES["preview"] == "xtts"
        # Every GPU type has a registered handler (guards against typos).
        assert jobs.GPU_JOB_TYPES <= set(jobs.HANDLERS)


class TestGpuSemaphore:
    def test_gpu_jobs_across_projects_do_not_overlap(self, isolated_data_dir, monkeypatch):
        """Two projects each submit a GPU job → executions never overlap."""
        import db
        import jobs

        p1 = _make_project()
        p2 = _make_project()

        in_flight = 0
        max_in_flight = 0
        done: list[str] = []

        async def slow_gpu(project_id, job_id, source_id, params):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.1)
            in_flight -= 1
            jobs._complete_job(project_id, job_id)
            done.append(project_id)

        monkeypatch.setitem(jobs.HANDLERS, "vocal_separation", slow_gpu)
        monkeypatch.setitem(jobs.HANDLERS, "diarisation", slow_gpu)

        async def run():
            job1 = jobs.enqueue(p1, "vocal_separation")
            job2 = jobs.enqueue(p2, "diarisation")
            for _ in range(100):
                if len(done) == 2:
                    break
                await asyncio.sleep(0.05)
            return job1, job2

        job1, job2 = asyncio.run(run())

        assert len(done) == 2, "both GPU jobs should have completed"
        assert max_in_flight == 1, "GPU jobs from different projects overlapped"
        for pid, jid in ((p1, job1), (p2, job2)):
            row = db.get_conn(pid).execute(
                "SELECT status FROM jobs WHERE id=?", (jid,)
            ).fetchone()
            assert row["status"] == "complete"

    def test_second_project_gpu_job_waits_for_lock(self, isolated_data_dir, monkeypatch):
        """Deterministic version: while project A holds the GPU lock, project
        B's GPU job must not start; it runs once A releases."""
        import jobs

        pa = _make_project()
        pb = _make_project()

        async def run():
            a_started = asyncio.Event()
            a_release = asyncio.Event()
            b_started = asyncio.Event()
            b_done = asyncio.Event()

            async def holder(project_id, job_id, source_id, params):
                a_started.set()
                await a_release.wait()
                jobs._complete_job(project_id, job_id)

            async def waiter(project_id, job_id, source_id, params):
                b_started.set()
                jobs._complete_job(project_id, job_id)
                b_done.set()

            monkeypatch.setitem(jobs.HANDLERS, "vocal_separation", holder)
            monkeypatch.setitem(jobs.HANDLERS, "transcription_bulk", waiter)

            jobs.enqueue(pa, "vocal_separation")
            await asyncio.wait_for(a_started.wait(), timeout=2)

            jobs.enqueue(pb, "transcription_bulk")
            # Give project B's runner ample time to (incorrectly) start.
            await asyncio.sleep(0.3)
            assert not b_started.is_set(), "GPU job started while another held the lock"

            a_release.set()
            await asyncio.wait_for(b_done.wait(), timeout=2)

        asyncio.run(run())

    def test_cpu_job_runs_while_gpu_lock_held(self, isolated_data_dir, monkeypatch):
        """A CPU job (extract_audio) in project B completes while a GPU job in
        project A holds the semaphore — CPU jobs are not gated."""
        import db
        import jobs

        pa = _make_project()
        pb = _make_project()

        async def run():
            gpu_started = asyncio.Event()
            gpu_release = asyncio.Event()
            gpu_done = asyncio.Event()
            cpu_done = asyncio.Event()

            async def gpu_handler(project_id, job_id, source_id, params):
                gpu_started.set()
                await gpu_release.wait()
                jobs._complete_job(project_id, job_id)
                gpu_done.set()

            async def cpu_handler(project_id, job_id, source_id, params):
                jobs._complete_job(project_id, job_id)
                cpu_done.set()

            monkeypatch.setitem(jobs.HANDLERS, "vocal_separation", gpu_handler)
            monkeypatch.setitem(jobs.HANDLERS, "extract_audio", cpu_handler)

            jobs.enqueue(pa, "vocal_separation")
            await asyncio.wait_for(gpu_started.wait(), timeout=2)

            # GPU lock is held by project A; project B's CPU job must still run.
            cpu_job = jobs.enqueue(pb, "extract_audio")
            await asyncio.wait_for(cpu_done.wait(), timeout=2)
            assert not gpu_done.is_set(), "GPU job should still be holding the lock"

            gpu_release.set()
            await asyncio.wait_for(gpu_done.wait(), timeout=2)
            return cpu_job

        cpu_job = asyncio.run(run())
        row = db.get_conn(pb).execute(
            "SELECT status FROM jobs WHERE id=?", (cpu_job,)
        ).fetchone()
        assert row["status"] == "complete"

    def test_per_project_fifo_unchanged(self, isolated_data_dir, monkeypatch):
        """Within one project, jobs still execute strictly in enqueue order
        with no overlap — GPU and CPU alike."""
        import jobs

        p = _make_project()
        order: list[tuple[str, str]] = []

        def make_handler(label):
            async def handler(project_id, job_id, source_id, params):
                order.append((label, "start"))
                await asyncio.sleep(0.05)
                order.append((label, "end"))
                jobs._complete_job(project_id, job_id)

            return handler

        monkeypatch.setitem(jobs.HANDLERS, "vocal_separation", make_handler("gpu1"))
        monkeypatch.setitem(jobs.HANDLERS, "extract_audio", make_handler("cpu"))
        monkeypatch.setitem(jobs.HANDLERS, "diarisation", make_handler("gpu2"))

        async def run():
            jobs.enqueue(p, "vocal_separation")
            jobs.enqueue(p, "extract_audio")
            jobs.enqueue(p, "diarisation")
            for _ in range(100):
                if len(order) == 6:
                    break
                await asyncio.sleep(0.05)

        asyncio.run(run())

        assert order == [
            ("gpu1", "start"), ("gpu1", "end"),
            ("cpu", "start"), ("cpu", "end"),
            ("gpu2", "start"), ("gpu2", "end"),
        ]
