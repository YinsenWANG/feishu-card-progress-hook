# Retired core patch instructions

The former run.py / conversation_loop.py patches are retired. This file is not an installation, upgrade, or patch-recovery procedure. No patch snippets are retained for application.

Use only the external gateway hook and optional native plugin observer described in README.md. Do not modify or monkeypatch Hermes core to obtain narration, usage, outcomes, or delivery events. Missing native fields remain visibly unknown; missing narration falls back to tool summaries.

Existing production patches are outside this repository's scope; this implementation has not changed or removed them. See NATIVE_CONTRACT.md for the checked source contracts and their limits.
