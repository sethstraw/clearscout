"""The local HTTP boundary: who may address this server, and who may write to it.

The parsers are tested directly because the shapes that matter are the ones a
browser or a proxy can send and a naive `startswith` would accept. The
request journeys prove the middleware actually consults them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InternalError

from clearscout.bootstrap.api import create_app
from clearscout.delivery.api.http_security import authority, origin_authority
from clearscout.infrastructure.persistence.migrations.versions import v0001_schema


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("localhost:8765", "localhost"),
        ("127.0.0.1:8765", "127.0.0.1"),
        # An IPv6 literal is bracketed once it carries a port.
        ("[::1]:8765", "::1"),
    ],
)
def test_every_loopback_spelling_is_accepted(value: str, host: str) -> None:
    """All three name this machine, and a browser picks between them itself."""
    assert authority(value, scheme="http") == (host, 8765)


@pytest.mark.parametrize(
    ("scheme", "expected_port"),
    [("http", 80), ("https", 443)],
)
def test_a_missing_port_takes_the_scheme_default(scheme: str, expected_port: int) -> None:
    assert authority("127.0.0.1", scheme=scheme) == ("127.0.0.1", expected_port)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "example.com",
        # Every one of these contains a loopback name, and none of them is one.
        "127.0.0.1.example.com",
        "localhost.example.com",
        "evil-localhost",
        "notlocalhost",
        # Credentials, a path, a query, or a fragment mean this is not an authority.
        "user@127.0.0.1",
        "user:secret@localhost",
        "127.0.0.1/admin",
        "127.0.0.1?x=1",
        "127.0.0.1#f",
        # Header smuggling: whitespace and commas split one header into two values.
        "127.0.0.1 evil.com",
        "127.0.0.1,evil.com",
        "127.0.0.1\tevil.com",
        # A port that is not a number is not a port.
        "127.0.0.1:notaport",
    ],
)
def test_anything_that_is_not_a_bare_loopback_authority_is_refused(value: str) -> None:
    assert authority(value, scheme="http") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:5173", ("http", "localhost", 5173)),
        ("http://127.0.0.1", ("http", "127.0.0.1", 80)),
        ("https://127.0.0.1", ("https", "127.0.0.1", 443)),
        # A bare slash is the whole path a real Origin ever carries.
        ("http://localhost/", ("http", "localhost", 80)),
    ],
)
def test_a_browser_origin_is_read_as_scheme_host_and_port(
    value: str, expected: tuple[str, str, int]
) -> None:
    assert origin_authority(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        # "null" is what a sandboxed frame sends, and it is not this origin.
        "null",
        "https://example.com",
        "http://127.0.0.1.example.com",
        # Schemes a page can hold but a local API must never trust.
        "file://localhost",
        "data:text/html,x",
        "javascript:alert(1)",
        # An Origin is scheme and authority only.
        "http://localhost/admin",
        "http://localhost?x=1",
        "http://user@localhost",
        "http://localhost:5173 http://evil.com",
    ],
)
def test_an_origin_that_is_not_this_machine_is_refused(value: str) -> None:
    assert origin_authority(value) is None


@pytest.fixture
def api(durable_database_url: str, tmp_path: Path):
    """The shipped application, addressed the way a browser would address it."""
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        yield client


def test_a_request_naming_someone_elses_host_is_refused_before_it_reaches_a_route(
    api: TestClient,
) -> None:
    """DNS rebinding arrives as a real request carrying somebody else's Host."""
    refused = api.get("/api/v1/health", headers={"Host": "evil.example.com"})

    assert refused.status_code == 400
    assert refused.json()["title"] == "Invalid Host"
    # The refusal carries the same headers a served response would.
    assert refused.headers["x-frame-options"] == "DENY"
    assert refused.headers["cache-control"] == "no-store"
    assert api.get("/api/v1/health").status_code == 200


def test_framework_method_errors_use_problem_documents(api: TestClient) -> None:
    """Framework failures keep the public error contract and correlation ID."""
    response = api.put("/api/v1/health")

    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:clearscout:problem:method-not-allowed"
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]


def test_an_unexpected_database_raise_keeps_the_problem_contract(
    durable_database_url: str, tmp_path: Path
) -> None:
    """A trigger that raises without an error code must not escape as a bare 500."""
    app = create_app(durable_database_url, tmp_path / "artifacts")

    async def _raises_internal() -> None:
        raise InternalError("SELECT 1", {}, Exception("audit_events is immutable"))

    # Ahead of the SPA catch-all, which otherwise answers every unmatched path.
    app.add_api_route("/api/v1/raises-internal", _raises_internal, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/api/v1/raises-internal")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:clearscout:problem:database-fault"
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    # The database's own wording never reaches the client.
    assert "immutable" not in response.text


def test_the_postgres_immutability_trigger_raises_a_constraint_error_code() -> None:
    """Without the code, Postgres sends InternalError where SQLite sends IntegrityError."""
    source = Path(v0001_schema.__file__).read_text(encoding="utf-8")
    postgres_branch = source.split('elif dialect == "postgresql":', 1)[1]
    reject_function = postgres_branch.split("CREATE TRIGGER", 1)[0]

    assert "RAISE EXCEPTION '% is immutable'" in reject_function
    assert "ERRCODE = '23514'" in reject_function


def test_a_cross_origin_write_is_refused_and_a_same_origin_one_is_not(api: TestClient) -> None:
    """A page on another origin may not make this server write."""
    body = {"slug": "boundary", "name": "Boundary"}

    refused = api.post("/api/v1/workspaces", json=body, headers={"Origin": "http://evil.example"})
    assert refused.status_code == 403
    assert refused.json()["title"] == "Cross-origin request refused"

    # Reading is not gated on Origin, because a read changes nothing.
    assert api.get("/api/v1/health", headers={"Origin": "http://evil.example"}).status_code == 200

    missing_header = api.post(
        "/api/v1/workspaces",
        json=body,
        headers={"Origin": "http://localhost"},
    )
    assert missing_header.status_code == 403
    assert missing_header.json()["title"] == "Browser mutation refused"

    allowed = api.post(
        "/api/v1/workspaces",
        json=body,
        headers={"Origin": "http://localhost", "X-ClearScout-Request": "browser"},
    )
    assert allowed.status_code == 201


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin", "X-ClearScout-Request": "browser"},
        {"Referer": "http://localhost/", "X-ClearScout-Request": "browser"},
    ],
)
def test_browser_indicators_without_an_origin_are_refused_but_local_clients_remain_supported(
    api: TestClient, headers: dict[str, str]
) -> None:
    """A request that identifies itself as browser traffic must carry an Origin."""
    body = {"slug": "boundary-client", "name": "Boundary client"}

    browser = api.post(
        "/api/v1/workspaces",
        json=body,
        headers=headers,
    )
    assert browser.status_code == 403
    assert browser.json()["title"] == "Browser mutation refused"

    client = api.post("/api/v1/workspaces", json=body)
    assert client.status_code == 201


def test_cross_site_fetch_metadata_is_refused_even_for_an_exact_origin(api: TestClient) -> None:
    """Fetch Metadata backs up the exact Origin check when browsers provide it."""
    response = api.post(
        "/api/v1/workspaces",
        json={"slug": "metadata", "name": "Metadata"},
        headers={
            "Origin": "http://localhost",
            "Sec-Fetch-Site": "cross-site",
            "X-ClearScout-Request": "browser",
        },
    )

    assert response.status_code == 403
    assert response.json()["title"] == "Browser mutation refused"


def test_frontend_root_asset_is_served_before_the_spa_fallback(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The favicon must be image bytes, not the application shell with a 200."""
    frontend = tmp_path / "web"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "brand").mkdir()
    (frontend / "index.html").write_text(
        "<!doctype html><title>ClearScout</title>", encoding="utf-8"
    )
    (frontend / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (frontend / "brand" / "mark.svg").write_text("<svg id='mark'></svg>", encoding="utf-8")
    monkeypatch.setattr("clearscout.bootstrap.api._frontend_directory", lambda: frontend)

    with TestClient(create_app(durable_database_url), base_url="http://localhost") as client:
        favicon = client.get("/favicon.svg")
        favicon_probe = client.head("/favicon.svg")
        brand_mark = client.get("/brand/mark.svg")
        application_route = client.get("/jobs")

    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert favicon.text == "<svg></svg>"
    assert favicon_probe.status_code == 200
    assert favicon_probe.headers["content-type"].startswith("image/svg+xml")
    assert brand_mark.status_code == 200
    assert brand_mark.headers["content-type"].startswith("image/svg+xml")
    assert brand_mark.text == "<svg id='mark'></svg>"
    assert application_route.text == "<!doctype html><title>ClearScout</title>"


@pytest.mark.parametrize("frontend_present", [False, True])
def test_openapi_discovery_returns_the_contract_before_the_frontend_route(
    durable_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frontend_present: bool,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>ClearScout</title>")
    monkeypatch.setattr(
        "clearscout.bootstrap.api._frontend_directory",
        lambda: frontend if frontend_present else None,
    )
    with TestClient(create_app(durable_database_url), base_url="http://localhost") as client:
        conventional = client.get("/openapi.json")
        versioned = client.get("/api/v1/openapi.json")

    assert conventional.status_code == 200
    assert conventional.headers["content-type"].startswith("application/json")
    assert conventional.json() == versioned.json()
    assert "/api/v1/workspaces/{workspace_id}/evaluation-preflight" in conventional.json()["paths"]
    assert "/api/v1/workspaces/{workspace_id}/spend-history" in conventional.json()["paths"]


def test_api_only_help_points_at_a_route_that_exists_without_the_spa(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API remains navigable when a distribution carries no frontend bundle."""
    monkeypatch.setattr("clearscout.bootstrap.api._frontend_directory", lambda: None)
    with TestClient(create_app(durable_database_url), base_url="http://localhost") as client:
        root = client.get("/")
        docs = client.get("/api/v1/docs", follow_redirects=False)
        missing = client.get("/not-an-application-route")

    assert root.json()["documentation"] == "/api/v1/openapi.json"
    assert docs.headers["location"] == "/api/v1/openapi.json"
    assert missing.status_code == 404
    assert missing.json()["type"] == "urn:clearscout:problem:not-found"


def test_api_errors_are_stable_and_do_not_echo_undeclared_fields(
    durable_database_url: str,
) -> None:
    with TestClient(create_app(durable_database_url), base_url="http://localhost") as client:
        secret = "do-not-echo-this-value"
        invalid = client.post(
            "/api/v1/workspaces",
            json={"slug": "api", "name": "API", "secret": secret},
        )
        assert invalid.status_code == 422
        assert invalid.headers["content-type"].startswith("application/problem+json")
        assert invalid.json()["detail"] == "The request did not match the documented API contract."
        assert secret not in invalid.text

        unavailable = client.get(
            "/api/v1/jobs/missing", params={"workspace_id": "missing-workspace"}
        )
        assert unavailable.status_code == 404
        assert "traceback" not in unavailable.text.lower()


def test_an_unknown_api_path_is_a_problem_document_not_the_application_shell(
    durable_database_url: str,
) -> None:
    """A mistyped endpoint has to fail as an API, not answer 200 with HTML."""
    with TestClient(create_app(durable_database_url), base_url="http://localhost") as client:
        missing = client.get("/api/v1/nonexistent")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/problem+json")
        assert missing.json()["correlation_id"]

        # A trailing slash is the same mistake.
        assert client.get("/api/v1/jobs/").status_code == 404

        # A future version prefix must not be served the current application.
        assert client.get("/api/v2/jobs").status_code in {404, 405}

        healthy = client.get("/api/v1/health")
        assert healthy.status_code == 200
        assert healthy.headers["content-type"].startswith("application/json")


def test_local_host_and_browser_origin_boundary(durable_database_url: str, tmp_path: Path) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        for host in (
            "localhost",
            "localhost:8765",
            "127.0.0.1",
            "127.0.0.1:8765",
            "[::1]",
            "[::1]:8765",
        ):
            assert client.get("/api/v1/health", headers={"Host": host}).status_code == 200

        for host in (
            "evil.example",
            "localhost.evil.example",
            "127.0.0.2",
            "0.0.0.0",  # noqa: S104 - hostile Host test, never a bind address
            "localhost:bad",
            "[::1",
            "localhost, evil.example",
            "user@localhost",
        ):
            refused = client.get("/api/v1/health", headers={"Host": host})
            assert refused.status_code == 400
            assert refused.headers["cache-control"] == "no-store"

        absent_origin = client.post(
            "/api/v1/workspaces",
            headers={"Host": "localhost:8765"},
            json={"slug": "non-browser", "name": "Non-browser client"},
        )
        assert absent_origin.status_code == 201
        for index, (host, origin) in enumerate(
            (
                ("localhost:8765", "http://localhost:8765"),
                ("127.0.0.1:8765", "http://127.0.0.1:8765"),
                ("[::1]:8765", "http://[::1]:8765"),
            )
        ):
            accepted = client.post(
                "/api/v1/workspaces",
                headers={
                    "Host": host,
                    "Origin": origin,
                    "X-ClearScout-Request": "browser",
                },
                json={"slug": f"browser-{index}", "name": f"Browser {index}"},
            )
            assert accepted.status_code == 201

        for origin in (
            "https://evil.example",
            "http://localhost.evil.example",
            "http://127.0.0.1:8765",
            "https://localhost:8765",
            "null",
            "http://localhost:9999",
        ):
            refused = client.post(
                "/api/v1/workspaces",
                headers={"Host": "localhost:8765", "Origin": origin},
                json={"slug": "refused", "name": "Refused"},
            )
            assert refused.status_code == 403
            assert refused.json()["title"] == "Cross-origin request refused"
        assert (
            client.get(
                "/api/v1/health",
                headers={"Host": "localhost:8765", "Origin": "https://evil.example"},
            ).status_code
            == 200
        )
