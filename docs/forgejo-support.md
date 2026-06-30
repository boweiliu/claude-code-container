# Plan: first-class private Forgejo (and generic self-hosted forge) support in `open-workspace`

## Why

The `open-workspace` service provider (`POST /open-workspace` in `server.py`) lets a
caller hand us `repo` + `ref` and get dropped into a terminal sitting in that checkout.
Today it transparently handles **private GitHub** repos by minting a short-lived
`repo`-scoped token via the openhost `oauth` service and injecting it into the
clone/fetch URL transiently (kept off disk).

That auto-auth path is **hard-gated to `github.com`**:

- `server.py:356` — the private-repo retry only runs when `_is_github(repo)` is true.
- `_is_github` (`server.py:298`) is literally `host == "github.com"` (or `*.github.com`).
- For any other host, `_resolve_access` skips token minting and classifies straight
  from git's error text → an auth failure becomes `403 forbidden` (`server.py:372-375`).

So a **private Forgejo** (self-hosted Gitea fork) repo can't be opened without the user
bringing their own credential. The two current workarounds both have downsides:

1. SSH URL — only works if a key is already loaded in `$HOME/.ssh` (probe runs
   `ssh -oBatchMode=yes`, `server.py:329`).
2. HTTPS with inline creds (`https://user:token@host/...`) — works, but for non-GitHub
   hosts there is **no token-stripping**, so the credential gets **persisted on disk** in
   `$HOME/<repo>/.git/config`. The off-disk guarantee only fires on the GitHub path
   (`WORKSPACE_GITHUB_TOKEN` is only set for github.com; see `open_workspace.sh:11-14`).

Goal: let a configured Forgejo host get the same transparent, off-disk private-repo
flow GitHub already has.

## What needs to change

The existing GitHub machinery is already 90% generic — it's just the *host gate* and the
*oauth provider name* that are GitHub-specific. The token-injection and off-disk handling
work for any http(s) host as-is.

1. **Generalize the host check.** Replace the boolean `_is_github(repo)` gate in
   `_resolve_access` (`server.py:356`) with a lookup of "do we have an oauth provider
   configured for this host?" Return the provider name (e.g. `github`, `forgejo`) or None.
   - Add config for the Forgejo host(s) + which oauth provider serves them. Likely an env
     var, e.g. `FORGEJO_HOSTS=forgejo.example.com,git.internal` plus a provider id, to
     avoid hard-coding. Keep `github.com` as a built-in default.

2. **Parameterize the token mint.** `_fetch_github_token` (`server.py:82`) hard-codes
   `{"provider": "github", "scopes": ["repo"]}` in the oauth call. Make `provider` (and
   scopes) a parameter so the same function mints a Forgejo token. Confirm the openhost
   `oauth` service actually supports a `forgejo` provider — **open question**, may need an
   oauth-side grant/registration before this is usable. If oauth doesn't support it, the
   fallback is a user-supplied PAT stored as a secret (see step 5).

3. **Token injection already works.** `_inject_github_token` (`server.py:303`) is
   host-agnostic — it injects into any http(s) authority and percent-encodes the token.
   Consider renaming to `_inject_token` since it's no longer GitHub-specific. No logic
   change needed.

4. **Off-disk on the clone side.** `open_workspace.sh` keys off `WORKSPACE_GITHUB_TOKEN`
   (`open_workspace.sh:11-14, 24-36, 106`). It builds an authed URL for the clone/fetch
   and always resets `origin` to the clean (token-free) URL afterward. This is already
   host-generic — only the **env var name** is GitHub-flavored. Rename to something neutral
   like `WORKSPACE_GIT_TOKEN` (set at `server.py:484-485`) and update both sides together.
   No behavioral change, just decouples it from "github".

5. **(Optional fallback) PAT-via-secrets.** If openhost `oauth` can't issue Forgejo
   tokens, support pulling a user-provided Forgejo PAT from the `secrets-v2` app (we
   already fetch secrets — see `_fetch_secrets` usage at `server.py:122`/`518`). Same
   injection + off-disk path; the only difference is where the token comes from.

## Touch points (file:line)

- `server.py:82` `_fetch_github_token` — parameterize provider/scopes.
- `server.py:298` `_is_github` — replace with host→provider resolution + config.
- `server.py:303` `_inject_github_token` — rename only; logic is fine.
- `server.py:341-375` `_resolve_access` — generalize the retry gate; keep GitHub behavior
  identical as a special case.
- `server.py:484-485` — env var name handed to the script.
- `open_workspace.sh:11-14, 24-36, 42, 53, 106` — rename `WORKSPACE_GITHUB_TOKEN`,
  no logic change.
- `test_open_workspace.py` — add Forgejo cases (public clone, private with token,
  private without grant → 403, token never lands in persisted `origin`).

## Invariants to preserve

- **Token never persisted on disk** for the auto-auth path — currently the strongest
  guarantee; must hold for Forgejo too. The inline-HTTPS-creds workaround that *does*
  persist is exactly what this work replaces.
- GitHub behavior unchanged — github.com stays a configured provider, same status codes.
- Contract status codes intact (`services/open-workspace/openapi.yaml`):
  `400` bad input, `403` private+no-auth, `404` missing repo/ref, `5xx` internal.
- SSH transport still can't take an injected token (`_inject_github_token` returns it
  unchanged for non-http) — that's fine, SSH-key auth is the path there.

## Open questions — resolved

1. **Does the openhost `oauth` service support a `forgejo` provider today?** No.
   `openhost/apps/oauth_provider/src/oauth_provider/core/providers.py` only ships
   `google`, `github`, `mock`, `mock_device`. Adding generic Forgejo is also non-trivial
   because each Forgejo instance needs its own registered OAuth app (no shared default
   like GitHub has). → **v1 uses the PAT-via-secrets-v2 fallback** (step 5). The oauth
   code path stays open for future providers (e.g. GitLab) — the new dispatch is generic.

2. **Config shape:** A general host→provider map via env, so other forges drop in
   identically. Single env var `WORKSPACE_GIT_HOSTS`, comma-separated `host=spec` where
   `spec` is one of:
   - `oauth:<provider>` — mint via openhost oauth-v2 (e.g. `oauth:github`).
   - `secret:<key>` — read a PAT from secrets-v2 under `<key>`.

   Built-in default: `github.com → oauth:github`. Env entries override/extend.

   Example deployment env for a private Forgejo at `git.example.com`:
   ```
   WORKSPACE_GIT_HOSTS=git.example.com=secret:FORGEJO_TOKEN_EXAMPLE
   ```
   …then the operator stores their Forgejo PAT in secrets-v2 under
   `FORGEJO_TOKEN_EXAMPLE`.

3. **Naming:** Committed to neutral names: `WORKSPACE_GIT_TOKEN` (env handed to the
   script), `_inject_token`, `_token_provider_for_host`, `_fetch_token_for`. GitHub-only
   `_is_github` is gone; GitHub is now just one configured host.
