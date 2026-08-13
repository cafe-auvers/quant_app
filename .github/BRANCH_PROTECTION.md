# Master Branch Protection

CI runs after direct pushes, but GitHub must require its checks to prevent a
broken commit from landing on `master`. Configure a branch rule or repository
ruleset in **Settings → Rules → Rulesets** (or **Settings → Branches** on the
legacy UI) targeting `master` with these required status checks:

- `test (3.11)`
- `test (3.12)`

For this single-maintainer repository, requiring a pull request is optional.
Requiring both status checks and preventing force pushes/deletions is
recommended. After saving the rule, open a small pull request and confirm its
merge button remains blocked until both matrix jobs pass.

Branch protection is GitHub-hosted state and cannot be enforced by
`.github/workflows/ci.yml` itself. Re-check this rule if the workflow/job name
or Python matrix changes, because required-check names must match exactly.
