# Quality Loop

Quality Loop is a Hermes Agent plugin for durable code-quality campaigns on the native Kanban board. It coordinates separate examination, implementation, and validation models while keeping workflow state deterministic and reviewable.

## Workflow

1. When commands are omitted, run a read-only **discovery preflight** that can select only commands
   derived by the controller from authenticated repository manifests.
2. **Examine** the current code once: score the five quality categories, choose one category tied for
   the lowest score, and return one evidence-backed atomic improvement touching at most two files.
3. **Execute** only that improvement with the coding model. The controller injects the fixed build/test
   commands, an exact file allowlist, and a repository snapshot into the card.
4. **Validate** independently with the validation model, then run the controller-owned deterministic
   build/test gates. The validator is read-only and never receives the gate command text.
5. On validation failure, create a direct bounded repair for the same atomic improvement and revalidate
   it, up to the configured repair limit.
6. After a passing change, start a fresh examination round. When the examiner returns
   `candidate_complete` (or the configured score target is reached), reuse the validation stage for a
   final read-only whole-codebase audit.
7. Finish only when the final validator passes **and** the configured build/test commands return exit
   code 0. Publication remains a separate explicit action.

The controller persists every transition so gateway restarts preserve ordering. Card idempotency
includes parent lineage and the authenticated improvement. The simplified workflow uses payload schema
v2 and card schema v4; older in-flight stages fail closed and must be restarted instead of being
reinterpreted.

The controller registers `on_kanban_dispatch_tick` and never calls an LLM itself.

## Requirements

- Hermes Agent with controller-owned Kanban task support
- `hermes_cli.kanban_db_controller.create_controller_task(..., handoff_contract=...)`
- Hermes Kanban enabled
- Rootless Podman 5 or newer on local Linux for the recommended isolated mode; remote Podman
  connections and Podman-machine mount paths are intentionally unsupported
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

- a clean existing project selected with the native folder browser, or entered as an absolute path;
- **Podman volume checkout** (recommended) plus a locally available image pinned as
  `registry/image@sha256:...`; the image must provide `/bin/sh` and the project's build tools;
- the assignee profile from the live Hermes profile picker;
- one shared provider and separate examination, execution, and validation models from that profile's live model catalog;
- at least one fixed build or test command;
- maximum rounds and repairs.

The native desktop page defaults to the currently active Hermes profile and loads
its configured provider and model catalog through the gateway. It supplements an
incomplete live response from the profile's non-secret model cache, keeps
provider-specific offline fallbacks, and permits manual IDs when no catalog is available.
Model and profile fields intentionally have no machine-specific defaults.

In Podman mode, the controller authenticates the source commit, creates a rootless
Podman volume, clones that exact commit into `repository/` in the volume, removes the
clone's `origin`, and points Kanban cards at that campaign checkout. The original source
workspace is never mounted into gate containers. Build and test gates run by local image
ID in disposable containers with all capabilities dropped, `no-new-privileges`, bounded
CPU/memory/PIDs, and no network by default. Select rootless outbound networking only for
gates that must fetch dependencies.

The Hermes Kanban worker itself still runs under the selected Hermes profile on the host;
Podman mode isolates campaign repository bytes and hard-gate execution, but is not a
general sandbox for every tool the model can call. Keep the worker profile's tool policy
least-privileged and do not expose the source workspace in card instructions.

Legacy direct-workspace mode executes build and test commands through the system shell
with the same permissions as Hermes. Only enter commands you trust.

## Publication and safety

Use a dedicated branch or worktree. By default, Quality Loop does **not** commit, push, merge, or deploy changes.

When **Enable manual publish after final PASS** is selected, a succeeded campaign exposes a separate
**Publish** action. Nothing is pushed automatically. The explicit action constructs one commit from
the authenticated campaign bytes and pushes that exact commit to the selected remote. In Podman mode it reads the
remote from the unchanged, re-authenticated source checkout; the isolated checkout has no
remote and receives no host Git credentials. The source checkout is not modified or
fast-forwarded. Before committing, the controller rejects common credential/key paths and
scans the diff for private keys and common API-token patterns. This screening is defense
in depth, not a substitute for reviewing the branch and its diff.

Campaign volumes are retained when a campaign stops or finishes so results remain
inspectable. Remove a terminal campaign's named `hermes-quality-loop-<id>` volume manually
after preserving anything needed for review.

Quality Loop never merges or deploys changes.

## Worker completion contract

Workers complete cards with `metadata.quality_loop.schema = "quality-loop/v2"`. Quality Loop checks
for the controller-owned Kanban task capability before persisting a campaign, so an incompatible
Hermes installation fails clearly without leaving a running campaign row. Missing or malformed stage metadata pauses the campaign as `needs_review` instead of guessing. The
controller reports the first precise contract error, including malformed score, evidence, scope, or
validation fields.

## Verification

From a Hermes Agent development checkout with its virtual environment active:

```bash
python -m unittest discover -s tests -v
hermes plugins validate . --install-deps --json
```

## License

MIT. See [LICENSE](LICENSE).
