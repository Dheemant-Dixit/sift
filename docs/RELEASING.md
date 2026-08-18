# Releasing

Pushing a `vX.Y.Z` tag publishes that version to PyPI and opens a GitHub
release. That is the whole procedure.

This document is longer than the procedure because the interesting parts are
the failure modes. PyPI **never lets a version number be reused** — not after a
yank, not after a delete — so a release is the one operation in this project
that cannot be undone. Almost everything below exists to make sure a mistake is
caught while it is still free.

---

## Contents

- [One-time setup](#one-time-setup-trusted-publishing) — required before the first release
- [Releasing](#releasing-1) — the five steps
- [Checking the setup without publishing](#checking-the-setup-without-publishing)
- [What actually runs](#what-actually-runs)
- [What the workflow refuses to do](#what-the-workflow-refuses-to-do)
- [What the repository refuses to do](#what-the-repository-refuses-to-do) — the rules outside the workflow
- [Prereleases](#prereleases)
- [If something goes wrong](#if-something-goes-wrong)
- [Changing the release workflow](#changing-the-release-workflow) — read before renaming anything
- [What this does not check](#what-this-does-not-check)

---

## One-time setup: trusted publishing

`.github/workflows/release.yml` does **not** use a PyPI API token. It uses
[trusted publishing](https://docs.pypi.org/trusted-publishers/): the job mints
a short-lived OIDC token proving it is *this workflow, in this repository,
running in this environment*, and PyPI exchanges that for an upload token
scoped to this project alone.

The practical difference is that there is no long-lived credential anywhere —
nothing in repository secrets to leak, nothing in `~/.pypirc` to accidentally
paste into a terminal, nothing to rotate. A token stolen from a laptop can
upload any version of the project from anywhere; an OIDC claim cannot be
replayed off a GitHub runner.

Register the publisher once, on
[pypi.org](https://pypi.org/manage/project/sift-downloads/settings/publishing/)
→ **Publishing** → **Add a new publisher** → **GitHub**:

| Field | Value |
|---|---|
| Owner | `Dheemant-Dixit` |
| Repository | `sift` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

> **Status:** registered and verified on 2026-08-16. If you are reading this on
> a fork, it is not registered for you — do it before tagging.

The environment name matters: the workflow declares `environment: pypi`, and if
PyPI is told to expect it, an upload from any *other* job in the repository is
rejected even if that job is otherwise legitimate.

**The `pypi` environment gates on a human.** Configured 2026-08-17 in GitHub →
Settings → Environments → `pypi`; no code change was needed, since the workflow
already declares `environment: pypi`. Everything up to the upload runs
unattended, and the irreversible step waits for a click.

| Setting | Value | Why |
|---|---|---|
| Required reviewers | `Dheemant-Dixit` | the upload waits for an explicit approval |
| Allow administrators to bypass | **off** | otherwise the approval is a prompt, not a gate |
| Prevent self-review | **off** | must stay off — see below |
| Deployment refs | tag `v*` **and** branch `main` | see below |

Two of those are load-bearing in a way that is easy to get backwards:

- **Prevent self-review must stay off.** There is one maintainer, and that
  maintainer is the only reviewer. Turning it on means no deployment can ever be
  approved by anyone, which does not make releases safer — it makes them
  impossible, permanently.
- **Branch `main` must stay in the deployment refs**, alongside the `v*` tags.
  It looks redundant, since a release only ever runs from a tag. But
  `verify-publisher` runs on `workflow_dispatch` from `main` and *must* live in
  the `pypi` environment (PyPI matches the environment name out of the token
  claims). Restrict this to tags and the release path still works while the only
  safe way to test it stops working.

Once trusted publishing is registered, **revoke any PyPI API token you were
using before.** Leaving it alive keeps exactly the risk trusted publishing was
adopted to remove: none of the rules on this page can see a `twine upload` run
from a laptop.

---

## Releasing

1. Bump `__version__` in `src/sift_downloads/__init__.py`. That is the only
   place a version is written — `pyproject.toml` reads it from there via
   `[tool.hatch.version]`, so the two cannot drift.
2. Add a `## X.Y.Z — YYYY-MM-DD` section to `CHANGELOG.md`. Its body becomes
   the GitHub release notes **verbatim**, so write it for a reader who has not
   seen the diff.
3. Merge both to `main` **through a pull request** with CI green. `main` is
   protected — see [what the repository refuses to
   do](#what-the-repository-refuses-to-do) — so there is no direct-push path,
   and the release's own ancestry guard means anything not on `main` cannot be
   published anyway.
4. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
5. **Approve the deployment.** When `preflight`, `CI` and `build` are green the
   `pypi` job stops and waits: Actions → the running release → **Review
   deployments** → `pypi` → *Approve and deploy*. Nothing is uploaded until
   this click, and it cannot be skipped — administrator bypass is off.

Watch it at Actions → Release. Roughly three to five minutes of machine time,
most of it the test matrix, plus however long the approval sits waiting. A
release will **not** finish while you are away from the keyboard; that is the
point of the gate, not a bug in it.

**Do not run `python -m build` or `twine upload` by hand.** A version published
outside the workflow has skipped every check below, and cannot be replaced.

### Before you tag, sanity-check locally

```bash
pytest            # must be green
pytest evals/     # answer quality — CI cannot run this, and a release should not skip it
```

The eval suite needs Ollama and is not part of CI, so a tag push will happily
publish a version whose answers got worse. This is the one quality gate that is
your responsibility rather than the workflow's.

---

## Checking the setup without publishing

```bash
gh workflow run Release
gh run watch "$(gh run list --workflow=Release --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Running the Release workflow by hand does **not** release. It runs only
`verify-publisher`, which mints the same OIDC token the publish job would,
exchanges it with PyPI, and throws the result away. Nothing is checked out,
built or uploaded, so it is safe to run whenever you want to know whether the
registration is still good.

**It now waits for a deployment approval too**, because it deliberately runs in
the `pypi` environment — which is exactly what makes it a faithful test. Approve
it the same way as a real release. `gh run watch` will appear to hang until you
do.

This is worth having because the registration is the one part of the release
path that lives outside this repository — reading `release.yml` cannot tell you
whether pypi.org agrees with it, and the alternative way to find out is to push
a tag and spend a version number on the answer.

On failure the job prints the claims GitHub asserted (`repository`,
`workflow_ref`, `environment`, `sub`) next to PyPI's refusal, which turns
"it doesn't work" into a diagnosis: compare them against the table above. On
success it prints nothing but a confirmation — the exchange really does return
a working upload token, and putting that in a log is the exact thing this whole
design exists to avoid.

The check has to live in `release.yml` and in the `pypi` environment rather
than in a workflow of its own. PyPI matches the workflow *filename* and the
environment name out of the token's claims, so a check running from anywhere
else would pass while the real publish still failed.

---

## What actually runs

```
tag push  ──▶ preflight ──┬──▶ CI (reusable call to ci.yml, 10 jobs)  ──┐
                          └──▶ build                                    ├──▶ pypi ──▶ github-release
                                                                        ┘
manual run ──▶ verify-publisher        (nothing else; all five above are skipped)
```

| Job | Does | Fails cost |
|---|---|---|
| `preflight` | four guards, ~15s | delete tag, re-push |
| `CI` | the same 10 jobs as a PR, on the tagged commit | delete tag, re-push |
| `build` | build, `twine check --strict`, filename check, content audit, clean-venv install | delete tag, re-push |
| `pypi` | trusted-publishing upload | see [partial upload](#the-upload-failed-partway) |
| `github-release` | `gh release create` with the changelog section and both artifacts attached | re-run the job |

The guards are ordered cheap-first on purpose: a mistyped tag stops in about
ten seconds rather than after the full matrix. `github-release` runs *after*
`pypi` so a release never announces a version that failed to upload.

---

## What the workflow refuses to do

A git tag is a *claim* that the code at that commit is version X.Y.Z, and
pushing one checks nothing. So:

- **The tag must parse** as `vMAJOR.MINOR.PATCH`, optionally `aN`/`bN`/`rcN`.
- **The tag must agree with `__version__`.** The failure this exists for is
  tagging `v0.3.0` while the package still says `0.2.0`. If the version happens
  to be one PyPI has never seen — say `0.2.1` — it would otherwise publish
  `0.2.1` under a release everyone reads as `0.3.0`, and neither number can be
  corrected afterwards.
- **The tagged commit must be an ancestor of `origin/main`.** A tag can be
  pushed from any commit, including one that never opened a pull request.
- **`CHANGELOG.md` must have a section for it.** Tagging before writing the
  release up is easy to do and invisible afterwards.
- **The full CI matrix must pass on the tagged commit**, not merely on main at
  some earlier point. `ci.yml` is called as a reusable workflow so it is the
  same ten jobs, against the tree actually being packaged.
- **The artifacts must carry the tagged version** in their filenames, must be
  exactly two, must pass `twine check --strict`, and the wheel must install into
  a clean virtualenv and report `sift X.Y.Z`. The test matrix runs against the
  source tree; only this runs against the thing users receive.
- **The artifacts must contain no `.npz`, no manifest and no `.env`.** sift's
  index holds the verbatim text of the user's documents, so one reaching PyPI
  would publish someone's bank statements. Nothing builds an index into the
  package and `.gitignore` covers them, but this is the one path where a
  mistake cannot be taken back. (`.env.example` is documentation and ships
  deliberately — the check is anchored so it does not catch it. An earlier
  unanchored version of this check would have failed every release.)

---

## What the repository refuses to do

Everything above is enforced by `release.yml`, which means it is enforced only
for things that go *through* `release.yml`. A guard written in a workflow cannot
stop someone from bypassing the workflow. These rules live in repository
settings instead, and apply to pushes themselves.

Configured 2026-08-17; the context count last changed 2026-08-18, when `lint`
joined the matrix. All of them have an **empty bypass list** — they apply to the
repository owner exactly as they apply to anyone else.

**The count in the table is not decoration.** Every context is named
individually in the ruleset, so adding a job to `ci.yml` does not add it to the
gate, and removing or renaming one leaves a required check that will never
report again — which blocks every merge, with no bypass. Change the two
together, or not at all.

| Where | Rule | Stops |
|---|---|---|
| `main` | pull request required, 0 approvals | pushing straight to the branch releases are cut from |
| `main` | all 11 CI contexts must pass, branch up to date | merging something the matrix has not seen |
| `main` | squash only, linear history | a merge commit making `--is-ancestor` ambiguous |
| `main` | no force-push, no deletion | rewriting history under an already-published tag |
| `refs/tags/v*` | no update, no force-move | a tag that no longer describes what PyPI shipped |
| `pypi` env | required reviewer, admins cannot bypass | an unattended upload |
| `pypi` env | deploys only from `v*` tags or `main` | a workflow on some other ref minting an upload token |

The tag rule is the one worth understanding. PyPI cannot un-publish a version,
so after a release the tag is the only durable record of *which commit* became
that version. If the tag can move, that record can be made to lie — silently,
and after the fact. Creating and deleting tags is still allowed, because the
[recovery path](#a-guard-failed-preflight-ci-or-build) needs it; only moving one
is refused.

**What is not enforced here:** the tag *name* pattern. `tag_name_pattern` is a
GitHub "metadata restriction", available only to organizations on Team or
Enterprise — on a personal repository the API rejects it with
`422 Invalid rule 'tag_name_pattern'`. So a malformed tag like `v1.0` can still
be created; it is caught about ten seconds later by preflight, and then has to
be deleted. The regex in `release.yml` is the only thing enforcing the format.

To change any of this: Settings → Rules → Rulesets, and Settings →
Environments → `pypi`. **If the CI matrix changes, update the required contexts
in the same place** — a required status check that never reports blocks every
merge, and with no bypass configured there is no way around it but to edit the
rule.

---

## Prereleases

Tags of the form `v0.3.0rc1`, `v0.3.0b2`, `v0.3.0a1` are supported and take the
same path. Two things differ:

- The GitHub release is marked **pre-release**, so it is not offered as the
  current version.
- `pip install sift-downloads` **ignores it**. Only `pip install --pre` or an
  exact pin picks it up. PyPI works this out from the version string; nothing
  in the workflow tells it.

`CHANGELOG.md` still needs a matching `## 0.3.0rc1` section — the guard does not
special-case prereleases.

Note that a prerelease still permanently spends that version string. Use one
when you genuinely want early feedback, not as a way to test the workflow — see
[what this does not check](#what-this-does-not-check).

---

## If something goes wrong

### A guard failed (preflight, CI, or build)

Nothing was published. Fix it on `main`, then **delete and re-create** the tag:

```bash
git tag -d vX.Y.Z && git push --delete origin vX.Y.Z
# ...fix, merge to main via PR...
git tag vX.Y.Z && git push origin vX.Y.Z
```

Delete-then-recreate, not `git push -f`. A `v*` tag cannot be moved — the
obvious shortcut is rejected by the remote:

```
git tag -f vX.Y.Z && git push -f origin vX.Y.Z
remote: - Cannot update this protected ref.
 ! [remote rejected] vX.Y.Z -> vX.Y.Z (push declined due to repository rule violations)
```

That rejection is deliberate and there is no bypass, including for the owner.
Deleting a tag is still allowed precisely so this recovery keeps working.

This is safe **only** because the guards run before the upload. Once a version
is on PyPI the tag must never be deleted or re-pointed at all — ship the
correction as the next version. That is why `0.1.0` was never published and the
corrected code went out as `0.1.1`.

### The upload failed partway

The one genuinely irreversible failure. `twine` uploads the wheel and the sdist
in one call; if one lands and the other does not, **the one that landed cannot
be replaced.**

Do not try to complete it by hand. Check what PyPI actually has:

```bash
pip index versions sift-downloads
```

If the version is present but incomplete, bump to the next patch and release
that. Deleting the file on PyPI does not free the filename for re-upload.

### The upload failed cleanly (nothing uploaded)

Re-running the failed job is safe — PyPI rejects a duplicate file rather than
overwriting one, so a retry cannot silently publish different bytes under the
same name.

### `Trusted publishing exchange failure`

The publisher registration on PyPI does not match. Run
[the verify job](#checking-the-setup-without-publishing) and compare its printed
claims against the setup table — the usual cause is the workflow *filename* or
the environment name.

### The GitHub release is missing or wrong

Harmless and fully recoverable: PyPI already has the version. Re-run the
`github-release` job, or create it by hand with `gh release create`.

### Yanking

Yanking on PyPI hides a version from new resolutions but does **not** free the
version number and does not remove the files. It is the right tool for "this
release is broken, don't install it", never for "I want to re-upload".

---

## Changing the release workflow

Four changes look harmless and are not. Each breaks releases silently — the
workflow keeps passing until the moment it has to publish.

1. **Renaming `release.yml`, or moving the publish job to another workflow
   file.** PyPI matches the *filename* out of the token claims. Rename it and
   every release fails at the exchange until the publisher is re-registered.
   Same for renaming the `pypi` environment.
2. **Removing `workflow_call` from `ci.yml`'s triggers.** `release.yml` calls
   it as a reusable workflow. Drop the trigger and the release's `CI` job fails
   to resolve.
3. **Renaming the default branch.** The ancestry guard hardcodes `origin/main`.
   The `pypi` environment's deployment refs also name `main` explicitly, and the
   branch ruleset follows the default branch automatically — so a rename leaves
   those two disagreeing.
4. **Tightening the `pypi` environment's deployment refs to tags only.** It
   looks like an obvious hardening, and it is the one change here that breaks
   the *check* rather than the release: `verify-publisher` dispatches from
   `main`, so it would start failing while real releases carried on working —
   removing the early warning for #1 exactly when it is most needed.

After any change to `release.yml`, run
[the verify job](#checking-the-setup-without-publishing) — it is free and it
catches #1 immediately.

Lint before pushing:

```bash
actionlint .github/workflows/*.yml
```

`actionlint` **silently skips** the shell and Python inside `run:` blocks unless
`shellcheck` and `pyflakes` are on `PATH`. A clean run without them means much
less than it looks like. Install both first.

Also: pass workflow context through `env:`, never interpolated into `run:` —
`${{ }}` is pasted in before the shell sees it. `GITHUB_REF_NAME` is already
available as an environment variable.

---

## What this does not check

Being honest about the edges:

- **Answer quality.** `evals/` needs a model server and is not in CI or in the
  release path. Run it yourself before tagging.
- **That the changelog is accurate** — only that a section exists.
- **Anything about the tag's signature or authorship**, beyond it being an
  ancestor of `main`.
- **The build and upload steps have not yet run for real.** As of 0.2.0 the
  credential handshake is verified and the tag trigger and preflight guards are
  verified (by a throwaway `v0.0.0` tag), but the reusable `CI` call, the build
  job, the upload and `gh release create` have never executed. The first real
  release will exercise them. Watch the `CI` job first — a reusable-workflow
  call that fails to resolve stops everything, and it is the piece with no prior
  runtime evidence at all.

That last point is deliberate. A test release would have proven the cheap-to-fix
steps while leaving the one irreversible risk — a partial upload — untested
anyway, in exchange for permanently spending a version number. Waiting was the
cheaper option because every guard fails *before* the upload.
