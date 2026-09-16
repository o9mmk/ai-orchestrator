# MVP acceptance matrix

`FINAL_DESIGN.md` §14のAT-1〜21を、次のpytest nodeで機械検証する。

| AT | 主なpytest node |
|---|---|
| AT-1 | `test_m9_cli.py::test_at1_cli_e2e_stops_for_approval_without_touching_user_tree` |
| AT-2 | `test_budget.py::test_at2_soft_budget_stops_new_start_after_running_child_finishes`, `test_planner.py::test_soft_budget_stops_planner_before_spawn` |
| AT-3 | `test_budget.py::test_at3_hard_budget_kills_all_ledgered_groups_and_checkpoints`, `test_m9_cli.py::test_cli_hard_budget_halts_after_completed_child_without_integration` |
| AT-4 | `test_child_runner.py::test_at4_timeout_kills_entire_pgid_and_records_boundary` |
| AT-5 | `test_child_runner.py::test_at5_invalid_json_is_rejected_without_manager_input`, `test_child_runner.py::test_schema_invalid_at_hard_cap_is_explicitly_escalated` |
| AT-6 | `test_completion.py::test_at6_claimed_done_cannot_override_failed_verification` |
| AT-7 | `test_resume.py::test_at7_stale_head_refuses_resume_without_rebase_or_new_run` |
| AT-8 | `test_state_store.py::test_at8_corrupt_checkpoint_replays_events_without_silent_repair` |
| AT-9 | `test_sandbox.py::test_secret_file_read_is_denied_without_exposing_value`, `test_manager_context.py::test_raw_logs_and_invalid_json_are_not_accepted_input_types` |
| AT-10 | `test_process_ledger.py::test_at10_live_orphan_pgid_is_recovered_and_event_is_recorded`, `test_m9_cli.py::test_resume_creates_new_generation_and_does_not_rerun_done_task` |
| AT-11 | `test_preflight.py::test_at11_dirty_scope_overlap_is_refused` |
| AT-12 | `test_preflight.py::test_dirty_nonoverlap_continues_with_warning` |
| AT-13 | `test_cancel.py::test_at13_cancel_kills_all_children_and_retains_worktree_artifacts` |
| AT-14 | `test_reviewer.py::test_at14_claude_unavailable_falls_back_to_independent_codex` |
| AT-15 | `test_m6_acceptance.py::test_at15_fake_child_secret_patch_blocks_claimed_done` |
| AT-16 | `test_completion.py::test_at16_second_request_changes_escalates_without_third_fix_cycle` |
| AT-17 | `test_preflight.py::test_at17_lease_contender_records_refused_with_no_llm_event`, `test_lease.py::test_fencing_mismatch_immediately_halts_old_store` |
| AT-18 | `test_sandbox.py::test_sandbox_exec_poc_blocks_network_and_outside_write`, `test_sandbox.py::test_external_symlink_and_hardlink_are_rejected` |
| AT-19 | `test_resume.py::test_tamper_refuses_resume_without_new_generation`, `test_state_store.py::test_at19_tamper_is_rejected_without_repair` |
| AT-20 | `test_baseline.py::test_at20_existing_failure_is_not_regression`, `test_baseline.py::test_at20_flipped_rerun_is_flaky` |
| AT-21 | `test_m6_acceptance.py::test_at21_fake_child_separates_clean_patch_from_polluted_outputs` |

補助的に、`test_m9_cli.py::test_gc_requires_exact_confirmation_and_retains_branch`で
確認文字列なしの削除拒否、run/worktreeだけの限定削除、integration branch保持を検証する。

## FABLE repair contracts

| Contract | 主なpytest node / opt-in smoke |
|---|---|
| Canonical / Codex transport schema分離 | `test_codex_transport_schema.py`, Planner/Attempt/Reviewの厳格fake provider |
| 実provider互換 | `uv run --isolated --locked python ai-orchestrator/tests/provider_smoke.py --codex /absolute/path/to/codex`（最大2 invocation） |
| duplicate run-id不変条件 | `test_preflight.py::test_duplicate_run_id_is_rejected_before_lease_or_existing_run_change` |
| dead owner回収 / live owner非強奪 | `test_lease.py::test_dead_lease_is_reclaimed_even_inside_ttl`, `test_lease.py::test_at17_second_run_is_refused_while_first_lease_is_live` |
| cross-terminal cooperative cancel | `test_cancel_intent.py`（0600/O_EXCL、PGID停止、二重cancel、terminal intent、approve競合） |
| lease renewal / fail-loud | `test_lease.py::test_active_store_renews_within_sixty_seconds_and_fails_loud_on_loss` |
| state dir fail-fast | `test_preflight.py::test_state_root_inside_repo_is_rejected_before_any_state_write` |
| cap / Planner事前検証 | `test_prelease_validation.py`, `test_cli_hardening.py::test_planner_hard_cap_below_two_fails_before_state_creation` |
| cleanup一次例外保全 | `test_preflight.py::test_stage1_failure_after_lease_releases_without_masking_primary`, `test_cli_hardening.py::test_cleanup_failure_does_not_mask_primary_and_is_not_silent_on_success` |
