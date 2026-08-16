# Releasing

Pushing a `vX.Y.Z` tag publishes that version to PyPI and opens a GitHub
release. That is the whole procedure — but it will not work until the one-time
setup below is done, and it is worth understanding what the workflow refuses to
do before relying on it.

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

The environment name matters: the workflow declares `environment: pypi`, and if
PyPI is told to expect it, an upload from any *other* job in the repository is
rejected even if that job is otherwise legitimate.

**Optional, recommended:** in GitHub → Settings → Environments → `pypi`, add
yourself as a required reviewer. Everything up to the upload then runs
unattended, and the irreversible step waits for a click. The workflow already
has the environment declared, so this needs no code change.

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

## Releasing

1. Bump `__version__` in `src/sift_downloads/__init__.py`. That is the only
   place a version is written — `pyproject.toml` reads it from there via
   `[tool.hatch.version]`, so the two cannot drift.
2. Add a `## X.Y.Z — YYYY-MM-DD` section to `CHANGELOG.md`. Its body becomes
   the GitHub release notes verbatim, so write it for a reader who has not seen
   the diff.
3. Merge both to `main` with CI green.
4. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

Watch it at Actions → Release. Nothing else is manual; in particular, do not
run `python -m build` or `twine upload` by hand any more. A version published
outside the workflow has skipped every check below.

## What the workflow refuses to do

A git tag is a *claim* that the code at that commit is version X.Y.Z, and
pushing one checks nothing. PyPI will never let a version number be reused —
not after a yank, not after a delete — so a wrong upload is permanent. Most of
the workflow is therefore about earning the right to upload:

- **The tag must parse** as `vMAJOR.MINOR.PATCH`, optionally `aN`/`bN`/`rcN`.
  Anything else stops in about ten seconds.
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
  same nine jobs, against the tree actually being packaged.
- **The artifacts must carry the tagged version** in their filenames, must pass
  `twine check --strict`, and the wheel must install into a clean virtualenv
  and report `sift X.Y.Z`. The test matrix runs against the source tree; only
  this runs against the thing users receive.
- **The artifacts must contain no `.npz`, no manifest and no `.env`.** sift's
  index holds the verbatim text of the user's documents, so one reaching PyPI
  would publish someone's bank statements. Nothing builds an index into the
  package and `.gitignore` covers them, but this is the one path where a
  mistake cannot be taken back. (`.env.example` is documentation and ships
  deliberately — the check is anchored so it does not catch it.)

The GitHub release is created **after** PyPI accepts, so a release never
announces a version that failed to upload.

## If something goes wrong

**A guard failed.** Fix it on `main`, delete and re-push the tag:

```bash
git tag -d vX.Y.Z && git push --delete origin vX.Y.Z
git tag vX.Y.Z && git push origin vX.Y.Z
```

This is safe precisely because nothing was published — the guards run before
the upload. Once a version is on PyPI, moving its tag is not an option; ship
the correction as the next version. That is why `0.1.0` was never published and
the corrected code went out as `0.1.1`.

**The upload failed but the guards passed.** Re-running the failed jobs is
safe: PyPI rejects a duplicate file rather than overwriting one, so a partial
upload cannot be silently completed with different bytes.

**`Trusted publishing exchange failure`.** The publisher registration on PyPI
does not match. Check the owner, repository, workflow *filename* (`release.yml`,
not the workflow's `name:`) and the environment name against the table above.
