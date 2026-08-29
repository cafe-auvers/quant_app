# Master Branch Protection

Last verified through the GitHub API on 2026-08-23: strict required status
checks are enabled for all three checks listed below, administrator enforcement
is enabled, and force pushes/deletions are blocked by the protected branch.

CI runs after direct pushes, but GitHub must require its checks to prevent a
broken commit from landing on `master`. Configure a branch rule or repository
ruleset in **Settings → Rules → Rulesets** (or **Settings → Branches** on the
legacy UI) targeting `master` with these required status checks:

- `test (3.11)`
- `test (3.12)`
- `Gate 1 deterministic simulation`

For this single-maintainer repository, requiring a pull request is optional.
`Gate 1 deterministic simulation` depends on both the repository-hygiene job
(including the secret scan) and the locked dependency audit. Because Gate 1 is
required, those checks are transitively release-blocking without adding more
hosted check names. Keep that dependency relationship intact when editing the
workflow.

Branch protection is GitHub-hosted state and cannot be enforced by
`.github/workflows/ci.yml` itself. Re-check this rule if the workflow/job name
or Python matrix changes, because required-check names must match exactly.
