# Publish a tagged dashboard image

Push a `v*` tag to Gitea for a local development image or to GitHub for the
public release image. Each workflow builds `dashboard/Dockerfile` from the
repository root and pushes one exact version tag for `linux/amd64`.
The image carries OCI version, commit revision and source labels.

CI runs before publishing a tag. For pull requests, add the `run-ci` label
to run the checks; subsequent commits rerun them while the label remains.
Remove the label to stop requesting CI. Ordinary branch pushes and unlabeled
pull requests allocate no CI runner. Pull requests never publish images.
Checks build the runtime image, run the full pytest suite and pinned Ruff in
a disposable Linux container, and check tracked files' ASCII punctuation.

| Remote | Default image | Workflow |
| --- | --- | --- |
| `origin` (Gitea) | `gitea.local/admin/deskmate:v1.2.3` | `.gitea/workflows/release-image.yml` |
| `github` | `ghcr.io/thegu/deskmate:v1.2.3` | `.github/workflows/release-image.yml` |

No `latest` tag is published. A tag must start with lowercase `v`, use only
letters, digits, underscores, dots or hyphens, and contain 1 to 128 characters.
Use distinct development tags such as `v1.2.3-dev.1`. Gitea treats container
tags case-insensitively, so never create tags differing only in letter case.

## Configure GitHub once

1. Enable Actions on the repository. The workflow requests `contents: read`
   and `packages: write` for its automatic `GITHUB_TOKEN`; no PAT is needed.
2. Optionally set repository Actions variable `CONTAINER_IMAGE` to an
   `owner/image` path. It defaults to the lowercase repository name.
3. After the first successful publication, open the package settings and
   change package visibility to **Public**. A public repository does not
   automatically make a new GHCR package public.

## Configure Gitea once

1. Enable Actions and the package registry on the Gitea instance/repository.
2. The workflow targets `ubuntu-24.04`, a label advertised by the existing
   online `unraid` runner. Verify that its job environment provides Git,
   GNU tar, Node.js 20 (for pinned checkout), Python 3.10 or newer and the
   Docker CLI, with access to a Linux Docker daemon. Run `docker info` from
   that environment. Both native and containerized runners are supported:
   CI copies tracked and nonignored source files into its test container.
3. Ensure the runner and Docker daemon resolve and reach Gitea, trust its
   HTTPS certificate, and can download public GitHub actions and base images.
   The Docker daemon must trust a private CA too. If the registry uses a
   separate host/port, set Actions variable `CONTAINER_REGISTRY` to that
   `host[:port]`, without a URL scheme or path. Otherwise the registry defaults
   to the authority in Gitea's configured server URL.
4. Expose the existing `DOCKER_REGISTRY_TOKEN` and `RELEASE_TOKEN` as Actions
   secrets available to this repository. `DOCKER_REGISTRY_TOKEN` needs
   `write:package` permission for the package owner. The registry username
   defaults to the repository owner (`admin` here); set Actions variable
   `DOCKER_REGISTRY_USERNAME` if the token belongs to another authorized
   account. The existing user variable sets it to `admin`.
   `RELEASE_TOKEN` is used only to check out publishing jobs, with fallback to
   the automatic job token. PR CI uses only the automatic job token. That job
   token cannot publish OCI images, so the registry PAT is required.
5. Optionally set Actions variable `CONTAINER_IMAGE` to `owner/image`.
   It defaults to the lowercase repository name.

The runner executes repository code with Docker access. Restrict it to
this trusted repository and restrict who can push release tags or apply the
`run-ci` label. Gitea CI accepts same-repository PRs only; fork PRs are skipped
even if labeled because the runner has access to the local Docker daemon.

## Publish

Complete the repository's checks in CONTRIBUTING.md, commit the release
changes, then create a tag on the intended commit. Push the commit/branch to
the chosen remote before pushing its tag:

```sh
git tag v1.2.3-dev.1 <commit>
git push origin refs/tags/v1.2.3-dev.1
# Watch Gitea Actions: Publish development image
```

For a public release, ensure the intended commit exists on GitHub:

```sh
git tag v1.2.3 <commit>
git push github refs/tags/v1.2.3
# Watch GitHub Actions: Publish release image
```

Pushing to one remote publishes only to that remote's registry. The workflow
fails on invalid input, a build failure, authentication failure or push
failure. Re-run a failed job after correcting runner access or credentials;
do not move a release tag to a different commit. Re-running a successful job
can overwrite that image tag, so use the reported registry digest when an
installation needs an immutable reference. No server is deployed by these jobs.

## Pull and run

For a private Gitea image, log in with a read-capable account first:

```sh
docker login gitea.local
docker pull gitea.local/admin/deskmate:v1.2.3-dev.1
```

For a public release:

```sh
docker pull ghcr.io/thegu/deskmate:v1.2.3
docker image inspect ghcr.io/thegu/deskmate:v1.2.3 --format '{{json .Config.Labels}}'
```

Keep the local-build Compose default. To use a published image, save a local
`compose.release.yml` override:

```yaml
services:
  dashboard-hub:
    image: ghcr.io/thegu/deskmate:v1.2.3
```

Then use the existing deployment settings and data mount:

```sh
docker compose -f docker-compose.yml -f compose.release.yml pull dashboard-hub
docker compose -f docker-compose.yml -f compose.release.yml up -d --no-build dashboard-hub
```

Rollback by setting the override to the previous tag or recorded digest and
running the same pull/up commands. Keep the data backup and schema migration
requirements in [DEPLOY.md](DEPLOY.md); selecting an older image does not undo
database migrations.

## References

- [GitHub package publication with GITHUB_TOKEN](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions)
- [GHCR visibility and repository links](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [Gitea Actions differences, including OCI token permissions](https://docs.gitea.com/usage/actions/comparison/)
- [Gitea container registry naming and authentication](https://docs.gitea.com/usage/packages/container/)
