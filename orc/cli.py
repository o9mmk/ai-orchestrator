"""User-facing `orc` CLI with no daemon, merge, push, or remote write path."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import shutil
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.budget_models import default_budget_caps, validate_budget_caps
from orc.cancel import CancelService
from orc.cancel_intent import CancelIntent, finalize_pending_cancel
from orc.gc import GcService
from orc.lease import LeaseManager
from orc.manager import ManagerOutcome, ManagerService, discover_codex, load_plan_file
from orc.paths import validate_state_root
from orc.plan_schema import validate_plan
from orc.preflight_models import PreflightConfig
from orc.resume import ResumeConfig, ResumeService
from orc.resume_workflow import continue_resumed_generation
from orc.run_snapshot import load_run_snapshot
from orc.session import release_run, reopen_run
from orc.state_machine import RunState
from orc.status import read_status
from orc.summary import SummaryService

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the six-command MVP surface."""
    parser = argparse.ArgumentParser(prog="orc", description="Local bounded AI orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start and execute one bounded run")
    _repo_argument(run)
    run.add_argument("--run-id")
    run.add_argument("--goal")
    run.add_argument("--task-file", type=Path)
    run.add_argument("--plan-file", type=Path, help="validated local plan; skips Planner")
    run.add_argument("--acceptance", action="append", default=[])
    run.add_argument(
        "--forbidden",
        action="append",
        default=[],
        help="repeat once per item, e.g. --forbidden .env --forbidden .git/config",
    )
    run.add_argument("--authority", action="append", default=[])
    run.add_argument(
        "--codex", metavar="EXECUTABLE_PATH", help="Codex executable path; no shell command"
    )
    run.add_argument(
        "--model-window-tokens", type=int, help="required and consumed only by Planner"
    )
    run.add_argument("--allow-claude", action="store_true")
    run.add_argument("--claude", type=Path)
    run.add_argument(
        "--cap",
        action="append",
        default=[],
        metavar="KEY=JSON_INTEGER",
        help=(
            "repeatable scalar override; Planner consumes one child invocation, "
            "e.g. --cap task_attempts_hard=2"
        ),
    )

    status = subparsers.add_parser("status", help="verify and show one run")
    _repo_argument(status)
    status.add_argument("run_id")

    approve = subparsers.add_parser("approve", help="approve start or final candidate")
    _repo_argument(approve)
    approve.add_argument("run_id")
    approve.add_argument("--decision", choices=("approve", "reject"), default="approve")
    approve.add_argument("--codex")
    approve.add_argument("--allow-claude", action="store_true")
    approve.add_argument("--claude", type=Path)

    cancel = subparsers.add_parser("cancel", help="stop all children and retain artifacts")
    _repo_argument(cancel)
    cancel.add_argument("run_id")

    resume = subparsers.add_parser("resume", help="create a verified new run generation")
    _repo_argument(resume)
    resume.add_argument("run_id", help="terminal source run")
    resume.add_argument("--new-run-id")
    resume.add_argument("--codex")
    resume.add_argument("--allow-claude", action="store_true")
    resume.add_argument("--claude", type=Path)
    resume.add_argument("--cap", action="append", default=[])
    resume.add_argument("--allow-cap-raise", action="store_true")

    gc = subparsers.add_parser("gc", help="irreversibly delete one confirmed terminal run")
    _repo_argument(gc)
    gc.add_argument("run_id")
    gc.add_argument("--confirm-run-id", required=True)
    gc.add_argument("--force-unmerged", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one finite command and emit one JSON object."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            payload = _run(args, parser)
        elif args.command == "status":
            payload = read_status(args.repo, args.run_id)
        elif args.command == "approve":
            payload = _approve(args)
        elif args.command == "cancel":
            payload = _cancel(args)
        elif args.command == "resume":
            payload = _resume(args)
        else:
            payload = asdict(
                GcService(args.repo).collect(
                    args.run_id,
                    confirm_run_id=args.confirm_run_id,
                    force_unmerged=args.force_unmerged,
                )
            )
    except Exception as error:  # CLI converts fail-loud exceptions to bounded stderr.
        safe = {"error": type(error).__name__, "reason": _safe_error_reason(error)}
        sys.stderr.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
        return 2
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    try:
        validate_state_root(args.repo)
    except ValueError as error:
        raise ValueError(str(error)) from error
    task = _task_input(args.task_file)
    goal = args.goal or task.get("goal")
    acceptance = args.acceptance or task.get("acceptance_criteria", [])
    if not isinstance(goal, str) or not goal or not acceptance:
        parser.error("run requires --goal and at least one --acceptance, or a task file")
    forbidden = args.forbidden or task.get(
        "forbidden", ["merge", "push", "deploy", "production changes"]
    )
    authority = args.authority or task.get("authority_sources", ["user"])
    run_id = args.run_id or _new_run_id()
    caps = _caps(args.cap)
    plan = load_plan_file(args.plan_file) if args.plan_file is not None else None
    if plan is None and caps["child_invocations_hard"] < 2:
        raise ValueError("Planner requires child_invocations_hard >= 2")
    if plan is None and args.model_window_tokens is None:
        raise ValueError("--model-window-tokens is required when Planner is used")
    codex = discover_codex(args.codex)
    codex.probe()
    manager = ManagerService(
        args.repo,
        codex,
        claude_executable=_claude_path(args),
    )
    config = PreflightConfig(
        run_id=run_id,
        goal=goal,
        acceptance_criteria=list(acceptance),
        forbidden=list(forbidden),
        authority_sources=list(authority),
        budget_source="bytes_proxy",
        reviewer_policy="claude-codex-none" if args.allow_claude else "codex-none",
        gates=["pytest", "gitleaks", "glassworm"],
        safety_policy_version="1.0",
        caps=caps,
        required_tools=("git", "gitleaks"),
        optional_tools=("claude",) if args.allow_claude else (),
    )
    store = None
    try:
        outcome, store = manager.start(
            config,
            plan=plan,
            model_window_tokens=args.model_window_tokens,
        )
        return asdict(outcome)
    finally:
        owned = store or manager.active_store
        if owned is not None:
            _release_preserving_primary(owned)


def _approve(args: argparse.Namespace) -> dict[str, Any]:
    validate_state_root(args.repo)
    snapshot = load_run_snapshot(args.repo, args.run_id)
    intent = CancelIntent(args.repo, args.run_id)
    pending_token = snapshot.manifest["fencing_token"]
    pending_cancel = intent.pending(pending_token)
    if pending_cancel:
        lease_manager = LeaseManager(args.repo)
        current = lease_manager.current()
        if (
            current is not None
            and current.run_id == args.run_id
            and lease_manager.pid_alive(current.pid)
        ):
            return asdict(
                ManagerOutcome(args.run_id, "CANCEL_PENDING", "cancel_requested")
            )
    state = RunState(snapshot.manifest["state"])
    if state not in {RunState.AWAITING_START_APPROVAL, RunState.AWAITING_APPROVAL}:
        raise ValueError(f"run is not awaiting approval: {state.value}")
    start_manager = None
    if (
        not pending_cancel
        and state is RunState.AWAITING_START_APPROVAL
        and args.decision == "approve"
    ):
        codex = discover_codex(args.codex)
        codex.probe()
        start_manager = ManagerService(
            args.repo,
            codex,
            claude_executable=_claude_path(args),
        )
    store = reopen_run(args.repo, args.run_id)
    try:
        if intent.clear(pending_token):
            cancelled_outcome = CancelService(store).cancel()
            SummaryService(store, ArtifactIngestor(store)).write(reason="cancel_requested")
            return {"run_id": args.run_id, **asdict(cancelled_outcome)}
        if finalize_pending_cancel(store):
            return asdict(ManagerOutcome(args.run_id, "CANCELLED", "cancel_requested"))
        if args.decision == "reject":
            target = store.transition("reject", reason="human_reject")
            SummaryService(store, ArtifactIngestor(store)).write(reason="human_reject")
            return asdict(ManagerOutcome(args.run_id, target.value, "human_reject"))
        target = store.transition("approve", reason="human_approve")
        if target is RunState.RUNNING:
            if start_manager is None:
                raise ValueError("approved start has no validated Manager")
            outcome, _ = start_manager.continue_run(store)
            return asdict(outcome)
        branch, merge_command = _integration_details(store.verify_events())
        SummaryService(store, ArtifactIngestor(store)).write(
            reason="human_approve",
            branch=branch,
            merge_command=merge_command,
        )
        return asdict(
            ManagerOutcome(args.run_id, target.value, "human_approve", branch, merge_command)
        )
    finally:
        _release_preserving_primary(store)


def _cancel(args: argparse.Namespace) -> dict[str, Any]:
    validate_state_root(args.repo)
    snapshot = load_run_snapshot(args.repo, args.run_id)
    state = RunState(snapshot.manifest["state"])
    intent = CancelIntent(args.repo, args.run_id)
    snapshot_token = snapshot.manifest["fencing_token"]
    intent_token = snapshot_token
    if state.terminal:
        intent.clear(snapshot_token)
        return {"run_id": args.run_id, "state": state.value, "terminated_children": 0}
    intent.request(snapshot_token)
    lease_manager = LeaseManager(args.repo)
    current = lease_manager.current()
    if current is not None and current.run_id == args.run_id and lease_manager.pid_alive(current.pid):
        if current.fencing_token != snapshot_token:
            intent.clear(snapshot_token)
            intent.request(current.fencing_token)
            intent_token = current.fencing_token
        verified = lease_manager.current()
        if (
            verified is not None
            and verified.run_id == current.run_id
            and verified.fencing_token == current.fencing_token
            and lease_manager.pid_alive(verified.pid)
        ):
            return {
                "run_id": args.run_id,
                "state": "CANCEL_PENDING",
                "terminated_children": 0,
            }
    latest = load_run_snapshot(args.repo, args.run_id)
    latest_state = RunState(latest.manifest["state"])
    if latest_state.terminal:
        intent.clear(latest.manifest["fencing_token"])
        return {
            "run_id": args.run_id,
            "state": latest_state.value,
            "terminated_children": 0,
        }
    store = reopen_run(args.repo, args.run_id)
    try:
        intent.clear(intent_token)
        outcome = CancelService(store).cancel()
        SummaryService(store, ArtifactIngestor(store)).write(reason="cancel_requested")
        return {"run_id": args.run_id, **asdict(outcome)}
    finally:
        _release_preserving_primary(store)


def _resume(args: argparse.Namespace) -> dict[str, Any]:
    validate_state_root(args.repo)
    source = load_run_snapshot(args.repo, args.run_id)
    source_plan = json.loads((source.run_dir / "plan.json").read_text(encoding="utf-8"))
    if not isinstance(source_plan, dict):
        raise ValueError("source plan must be a JSON object")
    validate_plan(source_plan)
    codex = discover_codex(args.codex)
    codex.probe()
    caps = deepcopy(source.manifest["caps"])
    _apply_cap_overrides(caps, args.cap)
    validate_budget_caps(caps)
    new_run_id = args.new_run_id or _new_run_id()
    resumed = ResumeService(args.repo).resume(
        args.run_id,
        ResumeConfig(
            new_run_id,
            source.manifest["safety_policy_version"],
            tuple(source.manifest["gates"]),
            caps=caps,
            allow_cap_raise=args.allow_cap_raise,
        ),
    )
    try:
        manager = ManagerService(
            args.repo,
            codex,
            claude_executable=_claude_path(args),
        )
        outcome = continue_resumed_generation(resumed.store, args.run_id, manager)
        return asdict(outcome)
    finally:
        _release_preserving_primary(resumed.store)


def _task_input(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("task file must contain a JSON object")
    return data


def _caps(items: list[str]) -> dict[str, Any]:
    caps = default_budget_caps()
    _apply_cap_overrides(caps, items)
    validate_budget_caps(caps)
    return caps


def _apply_cap_overrides(caps: dict[str, Any], items: list[str]) -> None:
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or key not in caps:
            raise ValueError(f"invalid cap override: {key}")
        if isinstance(caps[key], dict):
            raise ValueError("nested caps cannot be overridden by the CLI")
        caps[key] = json.loads(raw)


def _release_preserving_primary(store: Any) -> None:
    """cleanup失敗を記録し、一次例外がある場合はそれを保持する。"""
    primary = sys.exception()
    try:
        release_run(store)
    except Exception as cleanup_error:
        LOGGER.exception("lease release cleanup failed", exc_info=cleanup_error)
        if primary is None:
            raise
        primary.add_note(f"lease release failed: {type(cleanup_error).__name__}")


def _claude_path(args: argparse.Namespace) -> Path | None:
    if not getattr(args, "allow_claude", False):
        return None
    selected = args.claude or shutil.which("claude")
    if selected is None:
        return None
    return Path(selected)


def _integration_details(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for event in reversed(events):
        if event["type"] == "integration_candidate_ready":
            branch = event["data"].get("branch")
            command = event["data"].get("merge_command")
            return (
                branch if isinstance(branch, str) else None,
                command if isinstance(command, str) else None,
            )
    return None, None


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{secrets.token_hex(4)}"


def _repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=Path.cwd())


def _safe_error_reason(error: Exception) -> str:
    message = str(error)
    if len(message) > 300 or any(character in message for character in "\r\n\x00"):
        return "operation_failed"
    return message or "operation_failed"
