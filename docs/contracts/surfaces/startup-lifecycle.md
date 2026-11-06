# Startup lifecycle

## Boundary, consumers, and ownership

This contract covers startup and shutdown of the orchestrator process, including Telegram adapter readiness. It applies to operators starting the service through systemd or manually with `python -m orchestrator`. `orchestrator/__main__.py` owns process-level startup/shutdown; the Telegram adapter owns its bot session and polling task.

## Observable startup flow

The process loads persisted session state, constructs the configured adapters/modules, starts the reply server and idle sweeper, then calls `core.start()` once. Telegram startup sets each configured command-menu scope and checks bot identity before it creates its polling task. A successful `Bot.me()` is cached by aiogram and is reused by polling startup. After `core.start()` succeeds, stale bubble cleanup runs and a single startup notification is scheduled after a short delay.

Each Telegram startup API request is retried independently only when it raises aiogram `TelegramNetworkError`. The startup network budget is 300 seconds total, measured monotonically across menu setup and identity check. Retry delays grow exponentially from 1 second and cap at 30 seconds. A completed menu operation is not repeated when a later menu operation fails; the whole core/adapters/modules are never restarted as a retry strategy. Once polling is active, aiogram's existing `getUpdates` retry/backoff behavior applies to retrieval errors; the 300-second startup budget does not bound runtime polling or change its broader error handling.

## Interfaces and compatibility

Existing `main`, `OrchestratorCore.start()/close()`, Telegram adapter `start()/stop()`, and configured adapter/module interfaces are retained. No new configuration setting or systemd interface is introduced. The polling task does not own the bot session; adapter shutdown closes that session.

## Failure behavior and recovery

`TelegramNetworkError` during an individual startup request is retried within the shared 300-second budget. Budget exhaustion fails startup with a safe timeout error; the process exits unsuccessfully and any subsequent restart is governed by the external supervisor. This contract does not promise indefinite in-process recovery or change systemd restart policy.

Authentication, configuration, and programming errors are not treated as transient network failures and propagate without this retry loop. If `core.start()` fails, or SIGTERM arrives during startup/backoff, the startup task is cancelled as appropriate. The process independently attempts top-level cleanup of status bubbles, sessions, reply server, and core; failures in one stage are logged without skipping later stages, and the original startup exception remains the startup failure. `OrchestratorCore.close()` also guards each module and transport stop independently. A startup failure does not produce the startup notification. SIGTERM that wins startup also suppresses it. An exception inside an unguarded internal core cleanup operation can still interrupt later operations within `core.close()`.

## Security and session-state limits

Startup recovery does not reset or replay sessions, resubmit prompts, alter credentials/keys, or log bot tokens. Session state remains persisted using the existing mechanisms. Polling's ordinary update retrieval and message-processing semantics are unchanged.

## Non-goals and related documentation

This surface does not cover proxy, WireGuard, DNS, transport, or generic lifecycle redesign; it makes no guarantee that external systemd restart limits or policy change. See the user guide's [installation and startup notes](../../ORCHESTRATOR.md#установка) for operator actions and diagnostics.
