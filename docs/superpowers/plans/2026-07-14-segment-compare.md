# Segment vs Model-Output Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Compare panel in the Models section that synthesises an approved segment's exact transcript through a trained XTTS model and A/B-plays original vs clone.

**Architecture:** A compare is an ordinary `preview` job whose params carry a `segment_id` (spec: `docs/superpowers/specs/2026-07-14-segment-compare-design.md`). Orchestrator derives the text from the segment's transcript, excludes the target segment from conditioning pools, and surfaces `segment_id` in `GET /previews`. Frontend adds a ComparePanel beside PreviewPanel, reusing the lifted sampling controls and existing polling machinery.

**Tech Stack:** FastAPI + raw SQLite (orchestrator), React + TypeScript + Vite + vitest (frontend).

## Global Constraints

- Errors use `AppError` from `errors.py`, never `HTTPException`. Format `{"error": "snake_case", "message": "...", "detail": {}}`.
- No ORM — raw SQL. One SQLite DB per project.
- The effective transcript of a segment is `COALESCE(transcript_edited, transcript)` — use it for both derivation and search.
- Orchestrator test command (run from `services/orchestrator/`):
  `uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`
- Frontend test command (run from `frontend/`): `pnpm test -- --run`
- Commit after every green task. Branch: `feature/segment-compare`.

---

### Task 1: `q` filter on `GET /segments`

**Files:**
- Modify: `services/orchestrator/routers/segments.py` (list_segments, ~line 107)
- Test: `services/orchestrator/tests/test_segment_compare.py` (create)

**Interfaces:**
- Produces: `GET /projects/{id}/segments?q=<text>` — case-insensitive substring match on `COALESCE(transcript_edited, transcript)`, composed with all existing filters. `%`/`_` in `q` are literal.

- [ ] **Step 1: Write failing tests**

Create `services/orchestrator/tests/test_segment_compare.py`:

```python
"""Segment-vs-model-output comparison: q filter, preview segment_id,
conditioning exclusion. Conventions follow tests/test_wave_xtts.py."""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="complete"):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", status, now, now),
    )
    conn.commit()
    return source_id


def _insert_seg(conn, project_id, source_id, status="approved", confidence=0.9,
                start=0.0, end=10.0, transcript="hello world", transcript_edited=None,
                cleaned_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, export_path, cleaned_path, start_secs,
            end_secs, speaker_label, match_confidence, status, transcript,
            transcript_edited, flags, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", None,
         cleaned_path, start, end, "SPEAKER_00", confidence, status, transcript,
         transcript_edited, None, now, now),
    )
    conn.commit()
    return seg_id


def _set_reference(conn, project_id, pdir):
    ref = pdir / "reference.wav"
    ref.write_bytes(b"\x00" * 100)
    conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
    conn.commit()


class TestSegmentsQFilter:
    def test_q_matches_transcript_substring(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        hit = _insert_seg(conn, project, src, status="approved", transcript="the quick brown fox")
        _insert_seg(conn, project, src, status="approved", transcript="lazy dog")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert resp.status_code == 200
        segs = resp.json()["segments"]
        assert [s["id"] for s in segs] == [hit]

    def test_q_is_case_insensitive(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        hit = _insert_seg(conn, project, src, status="approved", transcript="Quick Brown Fox")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert [s["id"] for s in resp.json()["segments"]] == [hit]

    def test_q_matches_edited_transcript_over_original(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        # Edited transcript replaces the original for search purposes.
        _insert_seg(conn, project, src, status="approved",
                    transcript="quick", transcript_edited="slow")

        miss = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert miss.json()["segments"] == []
        hit = client.get(f"/projects/{project}/segments",
                         params={"status": "approved", "q": "slow"})
        assert len(hit.json()["segments"]) == 1

    def test_q_treats_like_wildcards_as_literals(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_seg(conn, project, src, status="approved", transcript="one hundred")
        hit = _insert_seg(conn, project, src, status="approved", transcript="100% sure")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "100%"})
        assert [s["id"] for s in resp.json()["segments"]] == [hit]

    def test_q_composes_with_status_filter(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_seg(conn, project, src, status="rejected", transcript="quick fox")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert resp.json()["segments"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `services/orchestrator/`):
`uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/test_segment_compare.py -v`
Expected: the `TestSegmentsQFilter` tests FAIL (q param ignored → wrong result sets).

- [ ] **Step 3: Implement the `q` filter**

In `services/orchestrator/routers/segments.py`, add `q: Optional[str] = None` to the `list_segments` signature (after `source_id`), and after the `_range_filter_conditions` block (around line 163) add:

```python
    if q:
        # Case-insensitive substring match on the effective transcript.
        # % and _ in the query are literals, not LIKE wildcards.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "COALESCE(seg.transcript_edited, seg.transcript) LIKE ? ESCAPE '\\'"
        )
        params.append(f"%{escaped}%")
```

- [ ] **Step 4: Run tests to verify they pass**

Same command as Step 2. Expected: all `TestSegmentsQFilter` tests PASS. Also run the full `tests/test_segments.py` to check no regression.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/routers/segments.py services/orchestrator/tests/test_segment_compare.py
git commit -m "feat(orchestrator): q transcript search filter on GET /segments"
```

---

### Task 2: Conditioning exclusion in `_resolve_conditioning`

**Files:**
- Modify: `services/orchestrator/jobs.py` (`_resolve_conditioning`, line 1921; `_handle_preview`, line 1995)
- Test: `services/orchestrator/tests/test_segment_compare.py` (append)

**Interfaces:**
- Consumes: `_insert_source` / `_insert_seg` / `_set_reference` helpers from Task 1's test file.
- Produces: `_resolve_conditioning(conn, project_row, project_id, source, segment_count, exclude_segment_id=None)` — when set, that segment is excluded from `segments_raw`/`segments_cleaned` pools; `reference_clip` unaffected. `_handle_preview` threads `params["segment_id"]` through as `exclude_segment_id`.

- [ ] **Step 1: Write failing tests**

Append to `services/orchestrator/tests/test_segment_compare.py`:

```python
class TestConditioningExclusion:
    def _prow(self, conn, project_id):
        return conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def test_excluded_segment_dropped_from_raw_pool(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, confidence=0.99)
        other = _insert_seg(conn, project, src, confidence=0.9)

        _src, refs = jobs._resolve_conditioning(
            conn, self._prow(conn, project), project, "segments_raw", 5,
            exclude_segment_id=target,
        )
        assert len(refs) == 1
        assert other in refs[0]
        assert all(target not in p for p in refs)

    def test_excluded_segment_dropped_from_cleaned_pool(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, cleaned_path="cleaned/t.wav")
        _insert_seg(conn, project, src, cleaned_path="cleaned/o.wav")

        _src, refs = jobs._resolve_conditioning(
            conn, self._prow(conn, project), project, "segments_cleaned", 5,
            exclude_segment_id=target,
        )
        assert refs == [r for r in refs if "cleaned/o.wav" in r]

    def test_exclusion_can_empty_the_pool(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src)

        with pytest.raises(LookupError):
            jobs._resolve_conditioning(
                conn, self._prow(conn, project), project, "segments_raw", 5,
                exclude_segment_id=target,
            )

    def test_reference_clip_unaffected(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src)

        _src, refs = jobs._resolve_conditioning(
            conn, self._prow(conn, project), project, "reference_clip", 5,
            exclude_segment_id=target,
        )
        assert refs[0].endswith("reference.wav")

    def test_handle_preview_threads_exclusion(self, client, project, isolated_data_dir):
        """A preview job with segment_id must not condition on that segment."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, confidence=0.99)
        other = _insert_seg(conn, project, src, confidence=0.9)

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "result": {"output_path": "x", "duration_secs": 1.0}}

        import asyncio, jobs
        from jobs import enqueue

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = enqueue(project, "preview",
                             params={"text": "hello world", "segment_id": target,
                                     "model_id": None,
                                     "conditioning": {"source": "segments_raw",
                                                      "segment_count": 5}})
            loop = asyncio.new_event_loop()
            loop.run_until_complete(jobs._execute_job(project, job_id))
            loop.close()

        refs = captured["payload"]["reference_wavs"]
        assert all(target not in p for p in refs)
        assert any(other in p for p in refs)
```

- [ ] **Step 2: Run tests to verify they fail**

`... python -m pytest tests/test_segment_compare.py::TestConditioningExclusion -v`
Expected: FAIL — `_resolve_conditioning() got an unexpected keyword argument 'exclude_segment_id'`.

- [ ] **Step 3: Implement exclusion**

In `services/orchestrator/jobs.py`:

1. Change the signature (line 1921):

```python
def _resolve_conditioning(
    conn, project_row: Any, project_id: str, source: str | None, segment_count: int,
    exclude_segment_id: str | None = None,
) -> tuple[str, list[str]]:
```

2. In `try_segments`, exclude the target from the pool. Replace the query construction with:

```python
    def try_segments(cleaned: bool):
        if cleaned:
            # Prefer the dataset cache; fall back to the export WAV when no
            # dataset build has cleaned this segment yet.
            col = "COALESCE(seg.cleaned_path, seg.export_path)"
            extra = "AND (seg.cleaned_path IS NOT NULL OR seg.export_path IS NOT NULL)"
        else:
            col = "seg.raw_path"
            extra = ""
        # A compare preview must not be conditioned on the segment it is
        # judged against.
        exclude_sql = "AND seg.id != ?" if exclude_segment_id else ""
        query_params: list = [exclude_segment_id] if exclude_segment_id else []
        query_params.append(segment_count)
        rows = conn.execute(
            f"""
            SELECT {col} AS p FROM segments seg
            WHERE seg.status NOT IN ('rejected', 'auto_rejected')
              AND seg.duration_secs BETWEEN 2 AND 12
              {extra}
              {exclude_sql}
            ORDER BY seg.match_confidence DESC
            LIMIT ?
            """,
            query_params,
        ).fetchall()
        if rows:
            return ("segments_cleaned" if cleaned else "segments_raw"), [_abs(r["p"]) for r in rows]
        return None
```

3. In `_handle_preview` (line ~2025), thread the param through:

```python
        _resolved, reference_wavs = _resolve_conditioning(
            conn, project, project_id, source, segment_count,
            exclude_segment_id=params.get("segment_id"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

`... python -m pytest tests/test_segment_compare.py tests/test_wave_xtts.py -v`
Expected: all PASS (test_wave_xtts.py guards against regressions in existing conditioning behaviour).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/jobs.py services/orchestrator/tests/test_segment_compare.py
git commit -m "feat(orchestrator): exclude compare target segment from conditioning pools"
```

---

### Task 3: `segment_id` on `POST /previews` + list surfacing

**Files:**
- Modify: `services/orchestrator/routers/previews.py`
- Test: `services/orchestrator/tests/test_segment_compare.py` (append)

**Interfaces:**
- Consumes: Task 2's `_resolve_conditioning(..., exclude_segment_id=...)`; test helpers from Task 1.
- Produces:
  - `POST /projects/{id}/previews` accepts `segment_id: Optional[str]`. When set, `text` is optional/ignored; the effective transcript is stored as `text` in job params alongside `segment_id`. 409 `segment_not_comparable` if the segment is missing or has no transcript. 422 if neither `text` nor `segment_id` given.
  - `GET /projects/{id}/previews` items include `"segment_id": <str | null>`.

- [ ] **Step 1: Write failing tests**

Append to `services/orchestrator/tests/test_segment_compare.py`:

```python
class TestPreviewSegmentId:
    def test_segment_id_derives_text_and_stores_params(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        # Two segments so the conditioning pool survives excluding the target.
        target = _insert_seg(conn, project, src, transcript="say this exactly")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        assert resp.status_code == 202
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["text"] == "say this exactly"
        assert p["segment_id"] == target

    def test_segment_id_uses_edited_transcript(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="machine words",
                             transcript_edited="human words")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["text"] == "human words"

    def test_segment_id_ignores_client_text(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="the real line")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target, "text": "something else"})
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["text"] == "the real line"

    def test_missing_segment_409(self, client, project, isolated_data_dir):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": str(uuid.uuid4())})
        assert resp.status_code == 409
        assert resp.json()["error"] == "segment_not_comparable"

    def test_segment_without_transcript_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript=None)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        assert resp.status_code == 409
        assert resp.json()["error"] == "segment_not_comparable"

    def test_neither_text_nor_segment_id_422(self, client, project):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews", json={})
        assert resp.status_code == 422

    def test_list_surfaces_segment_id(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="a line")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            client.post(f"/projects/{project}/previews", json={"segment_id": target})
            client.post(f"/projects/{project}/previews", json={"text": "plain preview"})

        resp = client.get(f"/projects/{project}/previews")
        previews = resp.json()["previews"]
        by_text = {p["text"]: p for p in previews}
        assert by_text["a line"]["segment_id"] == target
        assert by_text["plain preview"]["segment_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

`... python -m pytest tests/test_segment_compare.py::TestPreviewSegmentId -v`
Expected: FAIL — 422s where 202/409 expected (unknown field / missing required `text`), `segment_id` KeyError in list test.

- [ ] **Step 3: Implement**

In `services/orchestrator/routers/previews.py`:

1. Make `text` optional and add `segment_id` on `CreatePreviewRequest`:

```python
class CreatePreviewRequest(BaseModel):
    # Either free text or a segment to compare against. When segment_id is
    # set, text is ignored — the segment's transcript is synthesised so the
    # clone says exactly what the original says.
    text: Optional[str] = Field(default=None, min_length=1, max_length=500)
    segment_id: Optional[str] = None
    model_id: Optional[str] = None
    conditioning: ConditioningSpec = Field(default_factory=ConditioningSpec)
    # ... existing sampling fields unchanged ...

    @model_validator(mode="after")
    def _require_text_or_segment(self):
        if self.text is None and self.segment_id is None:
            raise ValueError("either text or segment_id is required")
        return self
```

Add `from pydantic import BaseModel, Field, model_validator` to the imports.

2. In `create_preview`, after the model-ready check, resolve the text:

```python
    text = body.text
    if body.segment_id:
        seg = conn.execute(
            "SELECT COALESCE(transcript_edited, transcript) AS t FROM segments "
            "WHERE id=? AND project_id=?",
            (body.segment_id, project_id),
        ).fetchone()
        if seg is None or not seg["t"]:
            raise AppError(
                409, "segment_not_comparable",
                "The segment does not exist or has no transcript.",
            )
        text = seg["t"]
```

3. Pass the exclusion into the pre-check and store both fields in params:

```python
        _resolve_conditioning(
            conn, project, project_id, body.conditioning.source,
            body.conditioning.segment_count, exclude_segment_id=body.segment_id,
        )
```

and in the `enqueue` params replace `"text": body.text` with:

```python
            "text": text,
            "segment_id": body.segment_id,
```

4. In `list_previews`, add to the appended dict:

```python
            "segment_id": p.get("segment_id"),
```

- [ ] **Step 4: Run the full orchestrator suite**

`... python -m pytest tests/ -v`
Expected: all PASS (existing preview tests confirm plain-text previews unaffected).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/routers/previews.py services/orchestrator/tests/test_segment_compare.py
git commit -m "feat(orchestrator): segment_id compare previews — transcript derivation + list surfacing"
```

---

### Task 4: Lift sampling controls out of PreviewPanel

**Files:**
- Create: `frontend/src/components/voice/sampling.tsx`
- Modify: `frontend/src/components/voice/PreviewPanel.tsx`
- Test: existing `frontend/src/components/voice/PreviewPanel.temperature.test.tsx` (unchanged — regression gate)

**Interfaces:**
- Produces: `sampling.tsx` exporting exactly what PreviewPanel currently declares privately:
  - `export interface SamplingParams { temperature: number; speed: number; top_k: number; top_p: number }`
  - `export const DEFAULT_SAMPLING: SamplingParams`
  - `export function SliderRow(props)` — same props as the current private one (id, label, min, max, step, value, decimals, hint, onChange)

- [ ] **Step 1: Move the code**

Cut `SamplingParams`, `DEFAULT_SAMPLING` (PreviewPanel.tsx lines ~21-33) and the `SliderRow` component (lines ~210-252) into `frontend/src/components/voice/sampling.tsx`, adding `export` to each. In `PreviewPanel.tsx` delete the moved code and import:

```ts
import { SamplingParams, DEFAULT_SAMPLING, SliderRow } from './sampling'
```

(Use `import type { SamplingParams }` if lint requires type-only imports — match the file's existing style.)

No behaviour change; this is a pure move so ComparePanel can share the controls.

- [ ] **Step 2: Run the frontend suite to verify no regression**

Run (from `frontend/`): `pnpm test -- --run`
Expected: all PASS, including `PreviewPanel.temperature.test.tsx`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/voice/sampling.tsx frontend/src/components/voice/PreviewPanel.tsx
git commit -m "refactor(frontend): lift sampling controls into shared voice/sampling module"
```

---

### Task 5: API client + types for compare

**Files:**
- Modify: `frontend/src/types/api.ts`, `frontend/src/api/client.ts`
- Test: type-level only — the frontend suite + `pnpm exec tsc --noEmit` gate it.

**Interfaces:**
- Produces:
  - `CreatePreviewRequest.text` becomes optional; new optional `segment_id?: string`.
  - `Preview` gains `segment_id: string | null`.
  - `GetSegmentsParams` gains `q?: string`.
  - No new client functions — `createPreview`, `getPreviews`, `getSegments`, `getSegmentAudioUrl`, `getPreviewAudioUrl` already cover it.

- [ ] **Step 1: Edit the types**

In `frontend/src/types/api.ts`:

```ts
// CreatePreviewRequest: change `text: string` to
  text?: string
// and add
  segment_id?: string

// Preview: add
  segment_id: string | null

// GetSegmentsParams: add
  q?: string
```

Check `api/client.ts` `getSegments` — its params serialisation must pass `q` through. It builds a query string from the params object; if it enumerates keys explicitly, add `q`; if it iterates generically, no change.

- [ ] **Step 2: Typecheck + suite**

Run: `pnpm exec tsc --noEmit && pnpm test -- --run`
Expected: clean. (`PreviewColumn` builds `{ text, ... }` bodies — `text` becoming optional is backwards-compatible.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/api.ts frontend/src/api/client.ts
git commit -m "feat(frontend): compare preview types — segment_id, optional text, q filter"
```

---

### Task 6: ComparePanel component + tests

**Files:**
- Create: `frontend/src/components/voice/ComparePanel.tsx`
- Test: `frontend/src/components/voice/ComparePanel.test.tsx` (create)

**Interfaces:**
- Consumes: Task 4's `sampling.tsx` exports; Task 5's types; `usePolling` hook; `createPreview`, `getPreviews`, `getSegments`, `getSegmentAudioUrl`, `getPreviewAudioUrl` from `api/client`.
- Produces: `export function ComparePanel({ projectId, models, advanced }: ComparePanelProps)` — same props shape as PreviewPanel.

- [ ] **Step 1: Write failing tests**

Create `frontend/src/components/voice/ComparePanel.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ComparePanel } from './ComparePanel'
import { createPreview, getPreviews, getSegments } from '../../api/client'
import type { Model, Segment } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { ...actual, createPreview: vi.fn(), getPreviews: vi.fn(), getSegments: vi.fn(), getProject: vi.fn() }
})

const model: Model = {
  id: 'model-1234567890', project_id: 'p1', status: 'ready', dataset_mode: 'approved',
  min_confidence: null, segment_count: 10, dataset_duration_secs: 120,
  dataset_manifest_path: 'models/m/dataset.json', checkpoint_dir: 'models/m',
  params: null, eval_loss: null, error: null,
  created_at: '2026-07-14T00:00:00Z', updated_at: '2026-07-14T00:00:00Z',
}

const seg: Segment = {
  id: 'seg-1', source_id: 's1', source_filename: 'ep01.mkv',
  start_secs: 0, end_secs: 5, duration_secs: 5, match_confidence: 0.95,
  transcript: 'the quick brown fox', transcript_edited: null,
  transcript_confidence: 0.9, status: 'approved', clipping_warning: false,
  flags: [], audio_url: '/projects/p1/segments/seg-1/audio',
}

const paginated = { segments: [seg], pagination: { page: 1, per_page: 50, total: 1, pages: 1 } }

beforeEach(() => {
  vi.mocked(createPreview).mockReset()
  vi.mocked(getPreviews).mockReset().mockResolvedValue({ previews: [] })
  vi.mocked(getSegments).mockReset().mockResolvedValue(paginated)
})

describe('ComparePanel', () => {
  it('lists approved segments and generates a compare preview for the selected one', async () => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'job-1', type: 'preview' } })
    render(<ComparePanel projectId="p1" models={[model]} />)

    // Picker fetches approved + auto_approved segments
    await waitFor(() => expect(getSegments).toHaveBeenCalled())
    const [, params] = vi.mocked(getSegments).mock.calls[0]
    expect(params).toMatchObject({ status: 'approved,auto_approved' })

    // Select the segment, then generate
    fireEvent.click(await screen.findByText(/quick brown fox/))
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))

    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({ segment_id: 'seg-1', model_id: 'model-1234567890' })
    expect(body.text).toBeUndefined()
  })

  it('passes the search text as q', async () => {
    render(<ComparePanel projectId="p1" models={[model]} />)
    await waitFor(() => expect(getSegments).toHaveBeenCalled())

    fireEvent.change(screen.getByPlaceholderText(/search transcripts/i), {
      target: { value: 'fox' },
    })
    await waitFor(() => {
      const calls = vi.mocked(getSegments).mock.calls
      expect(calls[calls.length - 1][1]).toMatchObject({ q: 'fox' })
    })
  })

  it('shows Original and Clone players when the preview completes', async () => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'job-1', type: 'preview' } })
    vi.mocked(getPreviews).mockResolvedValue({
      previews: [{ id: 'job-1', status: 'complete', text: 'the quick brown fox',
                   model_id: 'model-1234567890', conditioning: null,
                   segment_id: 'seg-1', created_at: '2026-07-14T00:00:00Z' }],
    })
    // Clone audio is blob-fetched; stub fetch.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(new Blob(['x'], { type: 'audio/wav' }))))
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:clone'), revokeObjectURL: vi.fn(),
    }))

    render(<ComparePanel projectId="p1" models={[model]} />)
    fireEvent.click(await screen.findByText(/quick brown fox/))
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))

    await waitFor(() => expect(screen.getByText('Original')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('Clone')).toBeTruthy())
    vi.unstubAllGlobals()
  })

  it('lists past compares (previews with a segment_id) as history', async () => {
    vi.mocked(getPreviews).mockResolvedValue({
      previews: [
        { id: 'old-1', status: 'complete', text: 'an old line', model_id: null,
          conditioning: null, segment_id: 'seg-9', created_at: '2026-07-13T00:00:00Z' },
        { id: 'plain', status: 'complete', text: 'not a compare', model_id: null,
          conditioning: null, segment_id: null, created_at: '2026-07-13T00:00:00Z' },
      ],
    })
    render(<ComparePanel projectId="p1" models={[model]} />)

    await waitFor(() => expect(screen.getByText(/an old line/)).toBeTruthy())
    expect(screen.queryByText(/not a compare/)).toBeNull()
  })

  it('disables generate with no ready model', async () => {
    render(<ComparePanel projectId="p1" models={[]} />)
    fireEvent.click(await screen.findByText(/quick brown fox/))
    expect((screen.getByRole('button', { name: /generate/i }) as HTMLButtonElement).disabled).toBe(true)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm test -- --run ComparePanel`
Expected: FAIL — module `./ComparePanel` not found.

- [ ] **Step 3: Implement ComparePanel**

Create `frontend/src/components/voice/ComparePanel.tsx`. Model it on PreviewPanel's structure and styling (same class names / inline patterns as the surrounding voice components):

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Model, Preview, Segment } from '../../types/api'
import {
  createPreview, getPreviews, getPreviewAudioUrl, getSegments,
  getSegmentAudioUrl, getProject,
} from '../../api/client'
import { usePolling } from '../../hooks/usePolling'
import { errorMessage } from '../../utils/errors'
import { SamplingParams, DEFAULT_SAMPLING, SliderRow } from './sampling'

interface ComparePanelProps {
  projectId: string
  models: Model[]
  advanced?: boolean
}

const POLL_MS = 3000
const COMPARE_TIMEOUT_MS = 10 * 60_000
const SEARCH_DEBOUNCE_MS = 300

type Phase = 'idle' | 'generating' | 'ready' | 'error'

function effectiveTranscript(seg: Segment): string {
  return seg.transcript_edited ?? seg.transcript ?? ''
}

export function ComparePanel({ projectId, models, advanced = false }: ComparePanelProps) {
  const readyModels = models.filter((m) => m.status === 'ready')

  // --- segment picker ---
  const [query, setQuery] = useState('')
  const [segments, setSegments] = useState<Segment[]>([])
  const [selected, setSelected] = useState<Segment | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      getSegments(projectId, {
        status: 'approved,auto_approved',
        ...(query ? { q: query } : {}),
        sort: 'duration',
        order: 'desc',
        per_page: 50,
      }, ctrl.signal)
        .then((res) => setSegments(res.segments))
        .catch(() => { /* aborted or transient — keep the previous list */ })
    }, SEARCH_DEBOUNCE_MS)
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [projectId, query])

  // --- model + sampling ---
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  useEffect(() => {
    if (!selectedModelId && readyModels.length) setSelectedModelId(readyModels[0].id)
  }, [readyModels, selectedModelId])
  const [sampling, setSampling] = useState<SamplingParams>(DEFAULT_SAMPLING)

  // --- generate / poll (same shape as PreviewPanel's PreviewColumn) ---
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [cloneUrl, setCloneUrl] = useState<string | null>(null)
  const [history, setHistory] = useState<Preview[]>([])
  const cloneUrlRef = useRef<string | null>(null)

  function revokeUrl() {
    if (cloneUrlRef.current) { URL.revokeObjectURL(cloneUrlRef.current); cloneUrlRef.current = null }
  }
  useEffect(() => revokeUrl, [])

  async function loadClone(id: string) {
    const res = await fetch(getPreviewAudioUrl(projectId, id))
    if (!res.ok) throw new Error('audio fetch failed')
    const blob = await res.blob()
    revokeUrl()
    const url = URL.createObjectURL(blob)
    cloneUrlRef.current = url
    setCloneUrl(url)
    setPhase('ready')
  }

  async function surfaceFailure(id: string) {
    try {
      const project = await getProject(projectId)
      const jobError = project.recent_failed_jobs.find((j) => j.id === id)?.error
      setError(jobError ?? 'Preview failed.')
    } catch {
      setError('Preview failed.')
    }
    setPhase('error')
  }

  const fetchPreviews = useCallback(() => getPreviews(projectId), [projectId])
  const handlePreviews = useCallback(
    (res: { previews: Preview[] }) => {
      setHistory(res.previews.filter((p) => p.segment_id !== null))
      if (!previewId) return
      const preview = res.previews.find((p) => p.id === previewId)
      if (!preview) return
      if (preview.status === 'complete') loadClone(preview.id).catch(() => surfaceFailure(preview.id))
      else if (preview.status === 'failed') surfaceFailure(preview.id)
    },
    [previewId], // eslint-disable-line react-hooks/exhaustive-deps
  )

  usePolling(fetchPreviews, {
    intervalMs: POLL_MS,
    enabled: phase === 'generating' && previewId !== null,
    onData: handlePreviews,
  })

  // Initial history load
  useEffect(() => {
    getPreviews(projectId)
      .then((res) => setHistory(res.previews.filter((p) => p.segment_id !== null)))
      .catch(() => {})
  }, [projectId])

  useEffect(() => {
    if (phase !== 'generating') return
    const timer = setTimeout(() => { setError('Timed out.'); setPhase('error') }, COMPARE_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [phase])

  async function handleGenerate() {
    if (!selected) return
    setPhase('generating')
    setError(null)
    setCloneUrl(null)
    try {
      const res = await createPreview(projectId, {
        segment_id: selected.id,
        model_id: selectedModelId,
        ...sampling,
      })
      setPreviewId(res.enqueued_job.id)
    } catch (e) {
      setError(errorMessage(e))
      setPhase('error')
    }
  }

  const canGenerate = selected !== null && selectedModelId !== null && phase !== 'generating'

  return (
    <div>
      {/* Segment picker */}
      <input
        type="search"
        placeholder="Search transcripts…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <ul>
        {segments.map((s) => (
          <li key={s.id}>
            <button type="button" onClick={() => setSelected(s)}
                    aria-pressed={selected?.id === s.id}>
              {effectiveTranscript(s)} <span>({s.duration_secs.toFixed(1)}s)</span>
            </button>
          </li>
        ))}
      </ul>

      {/* Model + knobs */}
      <select value={selectedModelId ?? ''} onChange={(e) => setSelectedModelId(e.target.value || null)}>
        {readyModels.map((m) => (
          <option key={m.id} value={m.id}>
            {`${m.dataset_mode === 'auto' ? 'Auto' : 'Reviewed'} · ${m.id.slice(0, 8)}`}
          </option>
        ))}
      </select>
      <SliderRow id="cmp-temperature" label="Temperature" min={0.05} max={1.5} step={0.05}
                 value={sampling.temperature} decimals={2}
                 onChange={(v) => setSampling((s) => ({ ...s, temperature: v }))} />
      <SliderRow id="cmp-speed" label="Speed" min={0.25} max={2} step={0.05}
                 value={sampling.speed} decimals={2}
                 onChange={(v) => setSampling((s) => ({ ...s, speed: v }))} />
      {advanced && (
        <>
          <SliderRow id="cmp-top-k" label="Top-K" min={1} max={100} step={1}
                     value={sampling.top_k} decimals={0}
                     onChange={(v) => setSampling((s) => ({ ...s, top_k: Math.round(v) }))} />
          <SliderRow id="cmp-top-p" label="Top-P" min={0.05} max={1} step={0.05}
                     value={sampling.top_p} decimals={2}
                     onChange={(v) => setSampling((s) => ({ ...s, top_p: v }))} />
        </>
      )}

      <button type="button" onClick={handleGenerate} disabled={!canGenerate}>
        {phase === 'generating' ? 'Generating…' : 'Generate comparison'}
      </button>
      {error && <p role="alert">{error}</p>}

      {/* A/B result */}
      {selected && (
        <div>
          <h4>Original</h4>
          <audio controls src={getSegmentAudioUrl(projectId, selected.id)} />
          {cloneUrl && (
            <>
              <h4>Clone</h4>
              <audio controls src={cloneUrl} />
            </>
          )}
        </div>
      )}

      {/* History */}
      {history.length > 0 && (
        <div>
          <h4>Past comparisons</h4>
          <ul>
            {history.map((p) => (
              <li key={p.id}>
                <span>{p.text}</span>
                {p.status === 'complete' && (
                  <>
                    <audio controls src={getSegmentAudioUrl(projectId, p.segment_id!)}
                           onError={(e) => { (e.target as HTMLAudioElement).style.display = 'none' }} />
                    <audio controls src={getPreviewAudioUrl(projectId, p.id)} />
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
```

**IMPORTANT — adapt, don't transplant:** before finalising, open `PreviewPanel.tsx` and match its actual JSX classes, wrapper structure, and `SliderRow` prop ranges/labels exactly (the slider ranges above must match the ones PreviewPanel uses; copy them verbatim from the moved `sampling.tsx` call sites). Match `usePolling`'s real option names from `hooks/usePolling.ts` — if its signature differs from `{ intervalMs, enabled, onData }`, follow the hook, not this sketch. The component structure, state flow, and API calls above are the contract; the markup details defer to the codebase.

History players use the segment audio URL directly; if the segment was deleted the `onError` handler hides the original player (spec's "original disabled" case).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm test -- --run ComparePanel`
Expected: all 5 PASS. Then full suite: `pnpm test -- --run` and `pnpm exec tsc --noEmit`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/voice/ComparePanel.tsx frontend/src/components/voice/ComparePanel.test.tsx
git commit -m "feat(frontend): ComparePanel — segment vs model output A/B"
```

---

### Task 7: Mount in ModelsSection

**Files:**
- Modify: `frontend/src/components/voice/ModelsSection.tsx`

**Interfaces:**
- Consumes: Task 6's `ComparePanel`.

- [ ] **Step 1: Mount the panel**

In `ModelsSection.tsx`, import `ComparePanel` and add a sibling SubSection after the existing Preview one:

```tsx
<SubSection title="Compare">
  <ComparePanel projectId={project.id} models={models} advanced={advanced} />
</SubSection>
```

- [ ] **Step 2: Full frontend suite + typecheck**

Run: `pnpm exec tsc --noEmit && pnpm test -- --run`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/voice/ModelsSection.tsx
git commit -m "feat(frontend): mount ComparePanel in Models section"
```

---

### Task 8: Final verification

- [ ] **Step 1: Full orchestrator suite** — command from Global Constraints, from `services/orchestrator/`. Expected: all PASS.
- [ ] **Step 2: Full frontend suite + typecheck** — `pnpm exec tsc --noEmit && pnpm test -- --run` from `frontend/`. Expected: all PASS.
- [ ] **Step 3: Code review, then merge** — squash-merge `feature/segment-compare` to `main` and push (project workflow: direct to main, no PR).
