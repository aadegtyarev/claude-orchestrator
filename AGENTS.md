# Project instructions

<!-- BEGIN ad-pi-addons contract workflow -->
## Contracts and feature work
- Read `docs/contracts/meta-contract.md` before implementing or reviewing a feature.
- Before implementation, agree on and save the approved feature contract under `docs/contracts/features/`; identify and read affected `docs/contracts/surfaces/` contracts.
- Give the exact contract paths to the planner, implementer, and reviewer. If working directly, use the same contracts as the source of truth.
- If implementation reveals a changed surface or acceptance scope, stop and get approval before proceeding. After verification, update affected surface contracts, mark the feature contract complete for final verification, then delete the temporary feature-contract file.
- Preserve existing instructions and surface contracts; never overwrite or delete them silently. Feature contracts are temporary task artifacts and must not remain after verified completion.
- Use available subagent roles only when configured. For nontrivial codebase or documentation reconnaissance, delegate bounded, read-only investigation to a suitable configured role when available; request relevant source paths and concise evidence, not whole-document dumps. Keep trivial lookups direct. The parent owns scope, decisions, synthesis, verification, and user communication. If no suitable role is available, work directly from the same contracts and checks, and state if independent review cannot be obtained. This rule does not require named Pi skills.
- End every task with a concise user-facing report of what changed, checks and outcomes, and remaining risks or blockers. Do not finish with tool output alone or end silently; this rule requires no installed skills.
<!-- END ad-pi-addons contract workflow -->

## Project-specific commands and conventions
- Stack and important paths: Python >=3.10; `orchestrator/__main__.py` assembles runtime; `core/app.py` and `adapters/` expose lifecycle; pytest files under `tests/`.
- Tests / typecheck / build: `python -m pytest`; `ruff check .`; `tests/run_all.sh` for script-mode test execution; CI targets Python 3.10 and 3.12.
- Formatting and style: Ruff, 110-character line length, E/F/W rules (`pyproject.toml`).
- Project-specific constraints: keep offline tests; do not expose credentials; sessions persist in place. For substantial changes, save a source-grounded scout plan under `docs/contracts/features/` before code and give its exact path to implementers/reviewers. After verified completion, keep durable behavior in surface contracts and remove temporary feature/plan contracts.
