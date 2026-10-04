# Quality Loop

Quality Loop is a Hermes Agent plugin for durable code-quality campaigns on the native Kanban board. It coordinates separate examination, implementation, and validation models while keeping workflow state deterministic and reviewable.

## Workflow

1. **Examine** the current codebase with the configured examination model.
2. Select exactly one highest-priority improvement. Prefer one focused executor card; when a safe
   implementation requires dependent steps, the examiner may define 2–5 ordered atomic slices.
3. **Execute one slice at a time** with the coding model. Only the current slice becomes ready.
4. **Validate every slice independently** with the validation model. The next executor depends on the
   preceding validator, so a later slice cannot start before the earlier slice passes.
5. On focused validation failure, create a bounded repair for that same slice and revalidate it.
6. After all slices pass, run one read-only **integrated validation** across the complete selected
   improvement. An integration failure creates a scoped repair that preserves validated slices.
7. Only after integrated validation passes, start a fresh examination and ranking round.
8. When the examiner returns `candidate_complete`, require a final whole-codebase validation.
9. Finish only when the final validator passes **and** the configured build/test commands return exit code 0.

Legacy single-item examiner handoffs continue to run as one focused executor/validator pair before
fresh examination. The controller persists the selected improvement and slice position so gateway
restarts preserve ordering. Card idempotency includes parent lineage and slice scope, preventing a
retry from reusing a stale task from a different slice.

The controller registers `on_kanban_dispatch_tick` and never calls an LLM itself.

## Requirements

- Hermes Agent 0.21.3 or newer
- Hermes Kanban enabled
- One configured Hermes worker profile
- Three model names that the profile's provider can route
- An existing Git worktree or repository with at least one deterministic build or test command

## Installation

After the catalog entry is accepted:

```bash
hermes plugins install quality-loop
```

For a direct installation from this repository, pin the exact commit you reviewed:

```bash
hermes plugins install https://github.com/janamis/hermes-quality-loop --ref FULL_40_CHARACTER_SHA
```

Then enable `quality-loop` for the profile you use with Hermes Desktop.

## Desktop use

Open **Capabilities → Plugins**, enable **Quality Loop**, and use the new sidebar page. Enter:

- an existing project or worktree selected with the native folder browser, or entered as an absolute path;
- the assignee profile from the live Hermes profile picker;
- one shared provider and separate examination, execution, and validation models from that profile's live model catalog;
- at least one fixed build or test command;
- maximum rounds and repairs.

The native desktop page defaults to the currently active Hermes profile and loads
its configured provider and model catalog through the gateway. It supplements an
incomplete live response from the profile's non-secret model cache, keeps
provider-specific offline fallbacks, and permits manual IDs when no catalog is available.
Model and profile fields intentionally have no machine-specific defaults.

The configured build and test commands are deliberately executed by the
system shell in the selected workspace, with the same permissions as Hermes.
Only enter commands you trust and inspect the target repository before starting
a campaign.

## Publication and safety

Use a dedicated branch or worktree. By default, Quality Loop does **not** commit, push, merge, or deploy changes.

When **Publish after final PASS** is explicitly enabled, the controller may stage repository changes, create one commit, and push the selected branch to the selected remote after the target score and final validation gates pass. Before committing, it rejects common credential/key paths and scans the staged diff for private keys and common API-token patterns. This screening is defense in depth, not a substitute for reviewing the branch and its diff.

Quality Loop never merges or deploys changes.

## Worker completion contract

Workers complete cards with `metadata.quality_loop.schema = "quality-loop/v1"`. Missing or malformed examination or validation metadata pauses the campaign as `needs_review` instead of guessing.

## Verification

From a Hermes Agent development checkout with its virtual environment active:

```bash
python -m unittest discover -s tests -v
hermes plugins validate . --install-deps --json
```

## License

MIT. See [LICENSE](LICENSE).
