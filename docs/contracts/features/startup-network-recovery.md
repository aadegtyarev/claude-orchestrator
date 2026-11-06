# Startup network recovery

Status: Approved

## Goal

Recover the orchestrator from transient Telegram startup network/DNS failures and clean up safely when startup fails, without changing live service state, session data, credentials, or unrelated proxy work.

## Scenarios and acceptance criteria

- If `core.start()` fails during initial startup, cleanup closes all clients, tasks, and servers and does not raise `UnboundLocalError`.
- A transient Telegram startup network failure recovers when connectivity returns, without duplicate adapters, pollers, menus, or startup notifications, and without relying on systemd restart-limit recovery.
- Retry/backoff is bounded and cancellable by SIGTERM; permanent auth, configuration, and programming errors do not retry indefinitely.
- Existing sessions, credentials, and prompt submission behavior remain unchanged.

## Non-goals

No proxy, WireGuard, or DNS changes; no session replay; no transport rewrite; no generic lifecycle framework; no unrelated cleanup or live-service changes.

## Affected surfaces

- Existing user-facing lifecycle/install guidance: [`README.md`](../../../README.md) and [`docs/ORCHESTRATOR.md`](../../../docs/ORCHESTRATOR.md).
- No formal surface contract exists yet. Intended durable contract after verification: [`startup-lifecycle.md`](../surfaces/startup-lifecycle.md).

## Interfaces and constraints

Work only in isolated worktree `/mnt/hdd/Develop/Hobby/claude-orchestrator-startup-recovery`, branch `fix/startup-network-recovery`, based on freshly fetched `origin/main` at `2e3044a`. Preserve live checkout's unrelated `.claude/` untracked content. Respect existing `main`, `core`, Telegram adapter lifecycle, and systemd interfaces; prefer the smallest justified change and do not add a configuration API without evidence and approval. Do not expose tokens in tests, logs, review, or live validation.

## Validation

Offline regression coverage for early startup failure/partial cleanup, transient failure recovery, permanent failure, and cancellation during delay; existing pytest, ruff, and script-mode checks; CI on Python 3.10 and 3.12 with secret scanning. After merge, verify installed-service startup via journal and API reachability without printing tokens.
