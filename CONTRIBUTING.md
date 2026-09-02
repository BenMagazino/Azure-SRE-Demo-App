# Contributing

This repository uses GitHub Flow: `main` is the single integration branch, work is completed on
short-lived branches, and releases are immutable snapshots identified by semantic-version tags.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Use
[GitHub Issues](https://github.com/BenMagazino/Azure-SRE-Demo-App/issues) for community support and
feature or defect discussions, and follow [SECURITY.md](SECURITY.md) for private vulnerability
reports. This is an independent personal project; participation is community-based and does not
create a Microsoft support relationship.

## Branches

Create every working branch from the latest `main`:

```powershell
git switch main
git pull --ff-only
git switch -c <owner>/<type>/<issue>-<description>
```

Use the developer's GitHub handle as `owner`. Supported branch types are:

| Type | Use |
| --- | --- |
| `feature` | New user-facing behavior |
| `fix` | Defect corrections |
| `docs` | Documentation-only changes |
| `chore` | Maintenance, automation, or repository configuration |

Examples:

```text
benmagazino/feature/42-edge-profile-picker
benmagazino/fix/57-response-plan-isolation
alex/docs/61-portable-installation
```

The branch owner identifies the initial primary handler. The pull request assignee and **Primary
handler** field are authoritative if ownership changes.

Push the branch as soon as useful work exists so it is backed up remotely:

```powershell
git push --set-upstream origin HEAD
```

Open a draft pull request for incomplete work. Keep branches short-lived, synchronize them with
`main`, and delete them after merge. Do not create permanent developer, integration, release, or tag
branches. Branch from another feature branch only for an intentional stacked change that cannot yet
target `main`.

## Pull requests

All changes to `main` go through a pull request. The primary handler should:

1. Assign the pull request to themselves.
2. Link the related issue when one exists.
3. Complete the validation and release-impact sections.
4. Resolve review conversations.
5. Merge only after required CI checks pass.

CI runs the Python tests, checks JavaScript syntax, builds the portable Windows ZIP, and retains the
ZIP plus its SHA-256 checksum as a 14-day workflow artifact.

## Releases

Releases use stable [Semantic Versioning](https://semver.org/) tags in the form
`vMAJOR.MINOR.PATCH`. Create a tag only from a tested commit already contained in `main`:

```powershell
git switch main
git pull --ff-only
git tag -a v0.1.0 -m "Azure SRE Agent Demo v0.1.0"
git push origin v0.1.0
```

Pushing the tag starts the release workflow. It verifies that the tag has the required format and
belongs to `main`, reruns validation, builds the package from that exact commit, and publishes a
GitHub Release containing:

```text
AzureSREAgentDemo-portable-win-x64.zip
AzureSREAgentDemo-portable-win-x64.zip.sha256
```

Never move, reuse, or rebuild an existing release tag. Correct a released defect on a new branch and
publish a new patch version instead.
