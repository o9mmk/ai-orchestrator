# M4 implementation design

## Change summary

M4 adds a Codex child-process adapter, strict `result.json` validation, bounded
process-group termination, and a fail-loud `worktrees.json` process ledger.
Planner, budget accounting, DLP, review, resume generations, integration, and
user-facing CLI orchestration remain outside this slice.

## Impact surface

- Direct: `result_schema.py`, `codex_adapter.py`, `process_ledger.py`,
  `child_runner.py`, `store.py`, `store_artifacts.py`, and M4 tests.
- Indirect: `RunStateStore` event/checkpoint integrity and the existing
  `Worktree` ownership boundary.
- External: `codex exec --help` and `codex --version` are probed. Tests use a
  fake executable and never invoke an LLM.

## Data flow and invariants

1. Input: a `Worktree`, task/role/attempt, prompt, and manifest caps.
2. Startup: validate any existing process ledger and recover live orphan PGIDs.
3. Probe: require the exact Codex flags used by the adapter.
4. Spawn: run only with the dedicated worktree as both OS cwd and Codex `--cd`.
5. Bound: record PID/PGID before waiting; timeout sends SIGTERM, then SIGKILL.
6. Ingest: parse only the final-message file, validate it with jsonschema and
   expected task/role/attempt, then copy only valid JSON into run state.
7. Output: return a bounded structured outcome containing no raw stdout,
   stderr, or invalid JSON body.

Invariants:

- No child path points at the user worktree.
- `claimed_status` is recorded but never drives completion.
- Invalid JSON/schema consumes the current attempt and yields an explicit
  ESCALATED-equivalent outcome at the manifest hard cap.
- Ledger JSON, PID/PGID identity, and worktree ownership mismatches are errors;
  they are never silently repaired.
- Manager-facing values contain only validated result fields and log digests.

## Edge cases

- Missing/version-drifted Codex flags stop before spawn.
- SIGTERM-responsive and SIGTERM-ignoring children produce distinct records.
- A malformed ledger stops recovery without overwriting the original bytes.
- A live PGID with mismatched leader identity is not killed.
- Temporary schema/result/log files are removed after ingestion; cleanup
  mismatches fail loudly.

## Verification

- Red/Green M4 tests: AT-4, AT-5, AT-10, valid-result ingestion, parent-tree
  immutability, and the SIGTERM/SIGKILL boundary.
- Full M1-M4 pytest suite with `ORC_STATE_DIR` set to a temporary directory.
- `ruff check .`, silent-exception scan, staged secret scan, and GlassWorm gate.
- End-to-end path: fake Codex -> PGID ledger -> timeout/result ingestion ->
  event hash chain -> checkpointed valid artifact.
