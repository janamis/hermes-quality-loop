# Quality Loop

Quality Loop is a Hermes Agent plugin for durable code-quality campaigns on the native Kanban board. It coordinates separate examination, implementation, and validation models while keeping workflow state deterministic and reviewable.

## Workflow

1. **Examine** the current codebase with the configured examination model.
2. **Execute** the highest-priority proposed improvement with the coding model.
3. **Validate** independently with the validation model.
4. On failure, create a bounded repair card and revalidate.
5. On pass, start the next examination round.
6. When the examiner returns `candidate_complete`, require a final whole-codebase validation.
7. Finish only when the final validator passes **and** the configured build/test commands return exit code 0.

The controller registers `on_kanban_dispatch_tick` and never calls an LLM itself. Card creation uses idempotency keys so gateway retries cannot duplicate a stage.

## Requirements

- Hermes Agent 0.21 or newer
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

- an existing absolute project or worktree path;
- the assignee profile;
- the examination, execution, and validation model names;
- at least one fixed build or test command;
- maximum rounds and repairs.

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
