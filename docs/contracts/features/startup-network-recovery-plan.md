# Scout plan: startup network recovery

Status: Source-grounded reconnaissance; implementation plan pending validation.

## Approved source and scope

Read `docs/contracts/features/startup-network-recovery.md`. This plan stays within startup cleanup/retry behavior; no proxy/DNS modifications or live-service operations.

## Verified source paths

- `orchestrator/__main__.py`
- `orchestrator/core/app.py`
- `orchestrator/adapters/telegram/adapter.py`
- `orchestrator/adapters/web/adapter.py`
- `docs/ORCHESTRATOR.md`
- `README.md`

## Source-grounded findings

- `orchestrator/__main__.py::main` constructs adapters, starts `start_reply_server`, and creates `_idle_sweeper` before entering its `try/finally`. Its `finally` assumes `startup_task` exists, but that task is assigned only after `await core.start()` and stale-bubble cleanup succeed; an exception from initial `core.start()` therefore causes cleanup itself to fail with an unbound local. Early setup failures before the `try` can also leave already-started resources without the shutdown path.
- `orchestrator/core/app.py::OrchestratorCore.start` starts transports serially, then modules. `close` independently stops modules and transports, swallowing/logging per-component stop errors; however `close` is not automatically invoked if `start` raises.
- `orchestrator/adapters/telegram/adapter.py::TelegramAdapter.start` awaits `_set_command_menu()` before creating the polling task. `_set_command_menu` calls Telegram APIs, so a network error can occur before the poller exists; retry handling must avoid duplicate setup and must not misclassify permanent failures. Its `stop` path and adapter resource ownership need inspection before selecting the retry boundary.
- Web has a separate lifecycle in `orchestrator/adapters/web/adapter.py`; startup recovery must not accidentally duplicate/tear down unrelated active transports.
- README and `docs/ORCHESTRATOR.md` document service installation/manual start and Telegram configuration, but no startup failure/recovery promise exists today.

## Minimal implementation investigation/plan

1. Inspect adapter `stop` semantics, reply server startup/cleanup, existing tests and CI scripts before editing code. Identify a single startup ownership boundary that includes resources allocated before `core.start()` and always permits idempotent shutdown; initialize optional task handles before entering that boundary.
2. Keep the retry loop restricted to Telegram startup exceptions demonstrably transient (network/DNS), using bounded backoff and stop-event cancellation. Preserve immediate failure for auth/config/programming errors. Decide the precise exception classification and retry bound from existing dependencies/error types; if contract does not determine a safe classification, pause for operator approval rather than retrying all exceptions.
3. Make each Telegram startup attempt cleanly reversible/idempotent, including partial API setup and polling task creation, while retaining one adapter instance and only one successful poller/menu/start notification. Keep web/core startup semantics unchanged except necessary cleanup orchestration.
4. Add offline tests at the `main`/adapter lifecycle seams for core-start early failure with cleanup, transient retry then success without duplicates, permanent error without repeated retry, cancellation during backoff, and partial startup cleanup. Assert sessions/credentials/prompt submission are not involved or altered.
5. Update README and `docs/ORCHESTRATOR.md` only for the verified user-visible startup/retry behavior. Run pytest, Ruff, script-mode tests, and CI/Python-version/secrets checks; update the proposed durable surface contract only after verification. Do not touch the installed service until merge approval.

## Validated dependency evidence and approved refinement

The installed venv at `/home/adegtyarev/appimages/claude-orchestrator/.venv` contains aiogram `3.30.0`. Its `Dispatcher._polling` begins with `await bot.me()` before entering `_listen_updates`; `_listen_updates` catches broad exceptions only around `getUpdates` and retries forever with `BackoffConfig(min_delay=1, max_delay=5, factor=1.3, jitter=0.1)`. `Dispatcher.start_polling` starts `_polling` in an internal task and waits for it; exceptions from the initial `bot.me()` are propagated through the returned coroutine. `Bot.me()` is cached after successful `get_me()`. The adapter has no Dispatcher startup/shutdown callbacks registered. Therefore prewarm `await bot.me()` in the awaited adapter `start()` using a bounded retry for `TelegramNetworkError`; after success, the poller uses cached identity and aiogram's existing unbounded backoff applies to `getUpdates`. Invoke polling with `close_bot_session=False`, because the adapter's `stop()` owns closing its session.

Approved implementation refinement from supervisor: keep retry inside Telegram adapter before poll task creation; use a monotonic 300-second total startup budget and exponential retry delays 1/2/4/8/16/30 seconds (cap 30), bounded each call by remaining budget; retry only `TelegramNetworkError`, retry only a menu API request that did not complete, and never repeat whole `core.start()`. In main, use initialized optional task/resource handles, race startup against SIGTERM, cancel/await startup on stop, then run every shutdown action independently so one cleanup failure cannot skip others. No generic readiness/fatal supervisor is needed because `bot.me()` is awaited before `core.start()` completes.

## Delegation contract paths

Pass these exact paths to implementer, reviewer, and any refresh after substantial approach changes:

- `docs/contracts/meta-contract.md`
- `docs/contracts/features/startup-network-recovery.md`
- `docs/contracts/features/startup-network-recovery-plan.md`
- `docs/contracts/surfaces/startup-lifecycle.md` (to be created after verified behavior; not yet present)
