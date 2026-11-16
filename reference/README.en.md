# English AGENTS injection reference component

This directory contains a runnable, standard-library Python reference component. It is not the proposed scheduler, a plugin installer, a host-capability doctor, or a security sandbox.

## Commands

Run from the extracted design package:

```sh
python3 reference/inject_agents.py inspect --project /absolute/project
python3 reference/inject_agents.py apply --project /absolute/project
python3 reference/inject_agents.py remove --project /absolute/project
```

The project must already exist and its path must be absolute. Omitting the action selects read-only `inspect`. On Windows, select an existing Python interpreter, for example `py -3`, and use an absolute Windows path.

The default policy source is `../spec/AGENTS.block.en.md` relative to the script. The default target is `AGENTS.md`. If an existing `AGENTS.override.md` is present, applying to the default target is refused because the result could be ineffective in Codex. An explicit existing override can be selected with `--target AGENTS.override.md`; the component never creates a new override.

That override behavior is conservative and host-neutral. ZCode's reviewed default loader does not use Codex's override precedence. A production multi-host installer must resolve each host's effective sources and use per-target ownership records or verified context injection. This reference component supports one active target per explicit project; it does not perform that full multi-host installation.

## Behavior

The component preserves existing bytes outside its managed markers, UTF-8 BOM, the selected newline convention, and the target's mode. It records a content digest, original-byte backup, and managed-block identity under the project's `.parallel-dev` directory. Repeated application of the same block is idempotent. An update replaces only a matching owned block. Removal preserves user edits outside that block and never restores the entire old backup over the current file.

Edited, duplicated, missing, or unowned markers require manual resolution. Symlinks, special files, ambiguous case variants, invalid backups, and another active mutation lock are rejected. A pending operation journal prevents a new mutation until its exact before/after state has been inspected. Metadata and backups are retained after removal.

The component intentionally refuses to guess recovery for an interrupted multi-file operation. It does not scan for repositories, modify global settings, grant Hook trust, spawn agents, or inspect the full instruction chain. It does not guarantee protection against a malicious process with the same operating-system authority racing filesystem operations. The complete runtime requires stronger platform-specific boundaries.

## Verification

```sh
python3 -B -m unittest discover -s reference -p test_inject_agents.py -v
```

The tests use temporary project directories. They cover read-only defaults, BOM/CRLF, idempotence, outside edits, inside conflicts, override handling, case collisions, symlinks, special files, policy upgrades, missing targets, backup corruption, pending journals, locks, explicit paths, and mode preservation. Their results do not establish real-host plugin loading, Windows/macOS acceptance, or scheduler correctness; consult `../VALIDATION.json` for the actual run.

Run `python3 -B reference/check_design.py` for bounded static checks of JSON syntax, instruction/template text, selected cross-file invariants, and local schema references. This command explicitly does not implement a complete JSON Schema validator or execute host hooks.
