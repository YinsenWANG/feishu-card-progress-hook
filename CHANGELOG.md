# Changelog

## Unreleased — recommend 3.4.0 after deployment review

This is a recommendation, not a release, tag, deployment, or published version.

- Replace core-patch instructions with an external native gateway hook and optional native interim narration observer.
- Fail closed for group/forum/unknown chats, retain private long-task steps and narration priority, and mark unavailable outcome/delivery/usage/cost explicitly unknown.
- Use active HERMES_HOME, sibling helper and current Python interpreter; pin every lark-cli command to cli_a93011294a39dbc6.
- Add atomic state writes and file locks, bounded replacement retries, durable one-shot send protection, retained finish retries and stale interruption. Remove unscoped text-message fallback and fix terminal CardKit replacement/title/color.
- Restrict price lookup to exact registered model names; label existing CNY rates as unverified historical snapshots.
- Add 26 standalone offline regressions and one real native registry/plugin/queue integration regression. All 27 passed with the checked native source; five Python files compile and git diff --check passes.

Native delivery outcomes and provider rates remain unverified; no real messages were sent. See NATIVE_CONTRACT.md for precise capability limits.
