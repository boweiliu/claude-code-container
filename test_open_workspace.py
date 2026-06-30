"""Tests for the open-workspace endpoint and its repo-access probe.

Run with: .venv/bin/python -m pytest test_open_workspace.py -q

The tests don't touch the network: `_resolve_access` is exercised by stubbing
the `git ls-remote` runner and the token fetch, and the route is exercised by
stubbing `_resolve_access` itself.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import pytest

import server


def run(coro):
    return asyncio.run(coro)


# ── pure helpers ───────────────────────────────────────────────────────────


def test_repo_dir_name():
    assert server._repo_dir_name("https://github.com/octocat/Hello-World.git") == "Hello-World"
    assert server._repo_dir_name("git@github.com:octocat/Hello-World.git") == "Hello-World"
    assert server._repo_dir_name("https://example.com/a/b/") == "b"


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://github.com/o/r.git", "github.com"),
        ("git@github.com:o/r.git", "github.com"),
        ("ssh://git@github.com/o/r.git", "github.com"),
        ("https://gitlab.com/o/r.git", "gitlab.com"),
    ],
)
def test_git_host(url, host):
    assert server._git_host(url) == host


def test_inject_token_https():
    assert (
        server._inject_token("https://github.com/o/r.git", "ghs_abc")
        == "https://ghs_abc@github.com/o/r.git"
    )


def test_inject_token_leaves_ssh_unchanged():
    # The token can't be applied to an ssh transport.
    assert server._inject_token("git@github.com:o/r.git", "ghs_abc") == "git@github.com:o/r.git"
    assert server._inject_token("ssh://git@github.com/o/r.git", "t") == "ssh://git@github.com/o/r.git"


# ── host → token-provider lookup ──────────────────────────────────────────


def test_github_is_builtin_oauth_provider(monkeypatch):
    # No env config — github.com still resolves to the built-in oauth provider.
    monkeypatch.delenv("WORKSPACE_GIT_HOSTS", raising=False)
    p = server._token_provider_for_host("github.com")
    assert p == server.TokenProvider(kind="oauth", name="github")
    # *.github.com inherits the same.
    assert server._token_provider_for_host("gist.github.com") == p


def test_unknown_host_has_no_provider(monkeypatch):
    monkeypatch.delenv("WORKSPACE_GIT_HOSTS", raising=False)
    assert server._token_provider_for_host("gitlab.com") is None
    assert server._token_provider_for_host("") is None


def test_env_declares_forgejo_pat_host(monkeypatch):
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", "git.example.com=secret:FORGEJO_TOKEN_EXAMPLE")
    p = server._token_provider_for_host("git.example.com")
    assert p == server.TokenProvider(kind="secret", name="FORGEJO_TOKEN_EXAMPLE")
    # Case-insensitive on host.
    assert server._token_provider_for_host("Git.Example.COM") == p


def test_env_can_override_github_builtin(monkeypatch):
    # Useful when oauth-v2 isn't installed in a particular deployment.
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", "github.com=secret:GH_PAT")
    p = server._token_provider_for_host("github.com")
    assert p == server.TokenProvider(kind="secret", name="GH_PAT")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "no-equals-sign",
        "=spec-with-no-host",
        "host=",
        "host=bogus-no-colon",
        "host=unknown-kind:foo",
        "host=oauth:",
    ],
)
def test_malformed_env_entries_are_ignored(monkeypatch, raw):
    # A typo in env must not break unrelated hosts (notably github.com).
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", raw)
    # github built-in still works.
    assert server._token_provider_for_host("github.com") == server.TokenProvider("oauth", "github")
    # The malformed entry doesn't accidentally produce a provider.
    assert server._token_provider_for_host("host") is None


def test_env_multiple_hosts(monkeypatch):
    monkeypatch.setenv(
        "WORKSPACE_GIT_HOSTS",
        "git.a.com=secret:A_TOK , git.b.com=secret:B_TOK",
    )
    assert server._token_provider_for_host("git.a.com").name == "A_TOK"
    assert server._token_provider_for_host("git.b.com").name == "B_TOK"


# ── _resolve_access classification ───────────────────────────────────────────


def _stub_ls_remote(monkeypatch, results):
    """Feed a queue of (rc, stdout, stderr) tuples to successive ls-remote calls."""
    calls = []
    queue = list(results)

    async def fake(repo, ref, token):
        calls.append((repo, ref, token))
        return queue.pop(0)

    monkeypatch.setattr(server, "_run_ls_remote", fake)
    return calls


def _stub_token(monkeypatch, token: str) -> None:
    """Make `_fetch_token_for` return a fixed token regardless of provider."""

    async def fake(provider):
        return token

    monkeypatch.setattr(server, "_fetch_token_for", fake)


def test_resolve_public_repo_ok(monkeypatch):
    _stub_ls_remote(monkeypatch, [(0, "abc\trefs/heads/main\n", "")])
    _stub_token(monkeypatch, "")
    access = run(server._resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "ok"
    assert access.token == ""


def test_resolve_named_ref_missing_is_404(monkeypatch):
    # Repo reachable (rc 0) but ls-remote returned no matching ref.
    _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(server._resolve_access("https://github.com/o/r.git", "nope-branch"))
    assert access.decision == "not_found"


def test_resolve_sha_ref_skips_ref_probe(monkeypatch):
    calls = _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(server._resolve_access("https://github.com/o/r.git", "a1b2c3d4"))
    # A sha can't be confirmed via ls-remote, so an empty result is still "ok"
    # and we pass no ref to the probe.
    assert access.decision == "ok"
    assert calls[0][1] is None


def test_resolve_private_github_with_token(monkeypatch):
    # Unauthenticated probe fails, authenticated probe succeeds.
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found"), (0, "abc\tHEAD\n", "")])
    _stub_token(monkeypatch, "ghs_tok")
    access = run(server._resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "ok"
    assert access.token == "ghs_tok"


def test_resolve_github_not_found_even_with_token(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "not found"), (128, "", "not found")])
    _stub_token(monkeypatch, "ghs_tok")
    access = run(server._resolve_access("https://github.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_private_github_no_token_is_forbidden(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found")])
    _stub_token(monkeypatch, "")
    access = run(server._resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_auth_error_is_forbidden(monkeypatch):
    # Unconfigured host falls back to git's own error text — a clear auth
    # error there means "forbidden".
    monkeypatch.delenv("WORKSPACE_GIT_HOSTS", raising=False)
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: Authentication failed for 'https://gitlab.com/o/r.git'")])
    access = run(server._resolve_access("https://gitlab.com/o/r.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_not_found(monkeypatch):
    monkeypatch.delenv("WORKSPACE_GIT_HOSTS", raising=False)
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository 'https://gitlab.com/o/ghost.git/' not found")])
    access = run(server._resolve_access("https://gitlab.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


# ── Forgejo (PAT-via-secrets) ────────────────────────────────────────────────


def test_resolve_forgejo_public_repo_ok(monkeypatch):
    # Even if a host is declared, a successful unauthenticated probe should
    # take the fast path without consulting secrets.
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", "forge.example=secret:FORGE_TOK")
    _stub_ls_remote(monkeypatch, [(0, "abc\trefs/heads/main\n", "")])
    access = run(server._resolve_access("https://forge.example/o/r.git", "main"))
    assert access.decision == "ok"
    assert access.token == ""


def test_resolve_forgejo_private_with_pat(monkeypatch):
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", "forge.example=secret:FORGE_TOK")
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found"), (0, "abc\tHEAD\n", "")])

    async def fake_secrets(keys):
        assert keys == ["FORGE_TOK"]
        return {"FORGE_TOK": "forgejo_pat_xyz"}

    monkeypatch.setattr(server, "_fetch_secrets", fake_secrets)
    access = run(server._resolve_access("https://forge.example/o/private.git", "main"))
    assert access.decision == "ok"
    assert access.token == "forgejo_pat_xyz"


def test_resolve_forgejo_private_without_pat_is_forbidden(monkeypatch):
    # Host is configured to require a PAT, but secrets-v2 doesn't have one —
    # treat it as private to us, same as the GitHub-no-grant case.
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", "forge.example=secret:FORGE_TOK")
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found")])

    async def fake_secrets(keys):
        return {}

    monkeypatch.setattr(server, "_fetch_secrets", fake_secrets)
    access = run(server._resolve_access("https://forge.example/o/private.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_forgejo_pat_doesnt_grant_access(monkeypatch):
    # PAT exists but doesn't actually see the repo — mirror the GitHub case:
    # "not_found" rather than "forbidden", so a typo'd repo URL doesn't read
    # as a permissions problem.
    monkeypatch.setenv("WORKSPACE_GIT_HOSTS", "forge.example=secret:FORGE_TOK")
    _stub_ls_remote(
        monkeypatch,
        [(128, "", "fatal: repository not found"), (128, "", "fatal: repository not found")],
    )

    async def fake_secrets(keys):
        return {"FORGE_TOK": "some_pat"}

    monkeypatch.setattr(server, "_fetch_secrets", fake_secrets)
    access = run(server._resolve_access("https://forge.example/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_network_error(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: unable to access: Could not resolve host: github.com")])
    access = run(server._resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


def test_resolve_timeout_is_error(monkeypatch):
    _stub_ls_remote(monkeypatch, [(124, "", "timed out")])
    access = run(server._resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


# ── route behavior ───────────────────────────────────────────────────────────


def _stub_access(monkeypatch, decision, token=""):
    async def fake(repo, ref):
        return server.RepoAccess(decision, token=token)

    monkeypatch.setattr(server, "_resolve_access", fake)


def _post(form=None, json=None, query=None):
    # Quart's test client treats json/form/data as mutually exclusive, so only
    # pass whichever the test actually set.
    kwargs: dict = {}
    if form is not None:
        kwargs["form"] = form
    if json is not None:
        kwargs["json"] = json
    if query is not None:
        kwargs["query_string"] = query

    async def go():
        client = server.app.test_client()
        return await client.post("/open-workspace", **kwargs)

    return run(go())


def test_missing_repo_is_400(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"ref": "main"})
    assert resp.status_code == 400


def test_missing_ref_is_400(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git"})
    assert resp.status_code == 400


@pytest.mark.parametrize("repo", ["file:///etc/passwd", "ext::sh -c id", "not-a-url", "/tmp/x"])
def test_bad_transport_is_400(monkeypatch, repo):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": repo, "ref": "main"})
    assert resp.status_code == 400


@pytest.mark.parametrize("ref", ["-rf", "--upload-pack=x", "a b", "a;b", "a$b", "../etc"])
def test_unsafe_ref_is_400(monkeypatch, ref):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": ref})
    assert resp.status_code == 400


def test_forbidden_is_403(monkeypatch):
    _stub_access(monkeypatch, "forbidden")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 403
    assert "Location" not in resp.headers


def test_not_found_is_404(monkeypatch):
    _stub_access(monkeypatch, "not_found")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 404


def test_internal_error_is_500(monkeypatch):
    _stub_access(monkeypatch, "error")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 500


def _only_pending() -> server.PendingSession:
    """Assert there is exactly one queued session and return it (unwrapping
    the (session, expires_at) tuple in `_pending`)."""
    assert len(server._pending) == 1
    sid = next(iter(server._pending))
    return server._pending[sid][0]


def test_success_redirects_303_with_location(monkeypatch):
    server._pending.clear()
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("/?session=")
    pending = _only_pending()
    assert pending.env["WORKSPACE_REPO"] == "https://github.com/o/r.git"
    assert pending.env["WORKSPACE_REF"] == "main"
    assert pending.env["WORKSPACE_DIR"] == "r"
    assert "WORKSPACE_GIT_TOKEN" not in pending.env
    # Legacy GitHub-only name must be gone — if anything still keys off it,
    # the rename hasn't happened on that side.
    assert "WORKSPACE_GITHUB_TOKEN" not in pending.env


def test_success_passes_token_to_pending(monkeypatch):
    server._pending.clear()
    _stub_access(monkeypatch, "ok", token="ghs_secret")
    resp = _post(form={"repo": "https://github.com/o/private.git", "ref": "abc1234"})
    assert resp.status_code == 303
    assert _only_pending().env["WORKSPACE_GIT_TOKEN"] == "ghs_secret"
    assert "WORKSPACE_GITHUB_TOKEN" not in _only_pending().env


# ── token URL injection: malformed/invalid token shapes ──────────────────────
#
# GitHub tokens today are `[A-Za-z0-9_]`, but the oauth provider is external —
# we can't statically guarantee what it returns. These tests pin that no
# adversarial or accidentally-malformed token can corrupt the URL or splice
# extra hosts/credentials into the authority section.


@pytest.mark.parametrize(
    "token, expected",
    [
        # The happy path: a real GitHub token shape passes through unchanged
        # (underscore is unreserved per RFC 3986, no encoding needed).
        ("ghs_AbCd1234", "https://ghs_AbCd1234@github.com/o/r.git"),
        # `@` in the token would otherwise let the value re-anchor the URL's
        # authority and point git at an attacker-chosen host.
        ("evil@attacker.com", "https://evil%40attacker.com@github.com/o/r.git"),
        # `:` would otherwise be parsed as a user:password separator.
        ("foo:bar", "https://foo%3Abar@github.com/o/r.git"),
        # `/` would otherwise terminate the authority section and shift the
        # rest of the token into the path, silently changing the repo path.
        ("a/b", "https://a%2Fb@github.com/o/r.git"),
        # `%` is the encoding sigil — must itself be encoded so a literal
        # `%` in the token isn't re-decoded as something else.
        ("pct%20", "https://pct%2520@github.com/o/r.git"),
        # Spaces and other whitespace must be encoded — a raw space in the URL
        # would make git reject the URL outright.
        ("a b", "https://a%20b@github.com/o/r.git"),
        # Newline: a non-encoding implementation could log/leak the URL with a
        # line break embedded in the credential. Belt-and-suspenders.
        ("a\nb", "https://a%0Ab@github.com/o/r.git"),
    ],
)
def test_inject_token_encodes_unsafe_chars(token, expected):
    assert server._inject_token("https://github.com/o/r.git", token) == expected


@pytest.mark.parametrize(
    "token",
    ["evil@attacker.com", "foo:bar", "a/b", "pct%20", "a b", "a\nb"],
)
def test_inject_token_preserves_host_under_unsafe_input(token):
    """No token shape may shift the URL's host away from github.com."""
    out = server._inject_token("https://github.com/o/r.git", token)
    parsed = urllib.parse.urlparse(out)
    assert parsed.hostname == "github.com"
    assert parsed.path == "/o/r.git"


@pytest.mark.parametrize(
    "token, expected",
    [
        # Happy path with a port — the port must survive the netloc rewrite.
        ("ghs_AbCd1234", "https://ghs_AbCd1234@github.com:8443/o/r.git"),
        # `@` in the token would otherwise shift the port (and the host) out
        # from under us — verify the same encoding holds here.
        ("evil@attacker.com", "https://evil%40attacker.com@github.com:8443/o/r.git"),
        # `:` in the token would otherwise look like the user:port separator
        # and re-anchor the port.
        ("foo:bar", "https://foo%3Abar@github.com:8443/o/r.git"),
    ],
)
def test_inject_token_preserves_port(token, expected):
    """Explicit-port URLs must round-trip with the port intact, including under
    unsafe token shapes that target the port/host boundary."""
    assert server._inject_token("https://github.com:8443/o/r.git", token) == expected


def test_inject_token_empty_token_still_safe():
    # An empty token shouldn't reach `_inject_token` (the caller checks
    # first), but if it ever did, the URL must remain syntactically valid.
    out = server._inject_token("https://github.com/o/r.git", "")
    parsed = urllib.parse.urlparse(out)
    assert parsed.hostname == "github.com"


# ── pending-session TTL & sweep ──────────────────────────────────────────────


def test_pending_sweep_drops_expired(monkeypatch):
    server._pending.clear()
    # Freeze time so we can advance it deterministically.
    now = [1000.0]

    class FakeLoop:
        def time(self):
            return now[0]

    monkeypatch.setattr(server.asyncio, "get_event_loop", lambda: FakeLoop())

    server._put_pending("old", server.PendingSession(command=["true"]))
    assert "old" in server._pending

    now[0] += server._PENDING_TTL_SECONDS + 1
    # Inserting a fresh entry sweeps the old one.
    server._put_pending("new", server.PendingSession(command=["true"]))
    assert "old" not in server._pending
    assert "new" in server._pending


def test_pop_pending_returns_none_for_expired(monkeypatch):
    server._pending.clear()
    now = [1000.0]

    class FakeLoop:
        def time(self):
            return now[0]

    monkeypatch.setattr(server.asyncio, "get_event_loop", lambda: FakeLoop())

    server._put_pending("sid", server.PendingSession(command=["true"]))
    now[0] += server._PENDING_TTL_SECONDS + 1
    # An attached websocket arriving after the TTL must not pick up a stale
    # session — it should fall through to the default shell instead.
    assert server._pop_pending("sid") is None


def test_pop_pending_unknown_returns_none():
    server._pending.clear()
    assert server._pop_pending("does-not-exist") is None
    assert server._pop_pending(None) is None


def test_accepts_json_body(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(json={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303


def test_accepts_query_params(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(query={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303


def test_accepts_get_with_query(monkeypatch):
    # GET is supported as a workaround for the openhost router's 302→/login
    # bounce, which demotes the eventual POST back to a GET. The same query
    # params that work on POST must also work on GET.
    _stub_access(monkeypatch, "ok")

    async def go():
        client = server.app.test_client()
        return await client.get(
            "/open-workspace",
            query_string={"repo": "https://github.com/o/r.git", "ref": "main"},
        )

    resp = run(go())
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("/?session=")


def test_debug_route_is_gone():
    async def go():
        client = server.app.test_client()
        return await client.get("/debug", query_string={"repo": "https://github.com/o/r.git"})

    resp = run(go())
    assert resp.status_code == 404
