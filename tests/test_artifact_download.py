from __future__ import annotations

import io
import urllib.request

from touchstone.hosted.runtime import _DropAuthorizationOnRedirect, _hostname

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": "Bearer ghs-secret-token",
    "User-Agent": "touchstone-agent",
}


def _redirect(from_url: str, to_url: str) -> urllib.request.Request | None:
    """Ask the handler what request it would make for one 302."""
    original = urllib.request.Request(from_url, headers=dict(_HEADERS))
    return _DropAuthorizationOnRedirect().redirect_request(
        original,
        io.BytesIO(b""),
        302,
        "Found",
        {"location": to_url},
        to_url,
    )


def test_the_api_token_never_reaches_signed_storage() -> None:
    """GitHub redirects an artifact download to a signed URL on another host.

    That host answers 401 InvalidAuthenticationInfo when the request also
    carries an Authorization header, which made every artifact look absent:
    no State Snapshot could be restored and no resume could find its candidate.
    """
    redirected = _redirect(
        "https://api.github.com/repos/acme/widgets/actions/artifacts/1/zip",
        "https://productionresultssa0.blob.core.windows.net/actions-results/x?sig=abc",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None
    # Everything else still travels; only the credential is dropped.
    assert redirected.get_header("User-agent") == "touchstone-agent"


def test_a_redirect_inside_github_keeps_the_token() -> None:
    redirected = _redirect(
        "https://api.github.com/repos/acme/widgets/actions/artifacts/1/zip",
        "https://api.github.com/repos/acme/widgets/actions/artifacts/1/zip?attempt=2",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") == "Bearer ghs-secret-token"


def test_host_comparison_ignores_case_and_missing_hosts() -> None:
    assert _hostname("https://API.GitHub.com/x") == "api.github.com"
    assert _hostname("not a url") == ""
