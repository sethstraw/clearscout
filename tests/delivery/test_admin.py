"""The mounted model admin: reachable, complete, and read-only for every table.

The admin is generated from the mapper registry rather than a hand-written list,
so the tests assert reachability and the write boundary through real requests
instead of inspecting the views. `audit_events` is in `IMMUTABLE_TABLES` and
`job_postings` is not, and both are read-only in the admin: the write refusal is
the admin's own, not a side effect of the append-only triggers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clearscout.bootstrap import api as api_module
from clearscout.bootstrap.api import ADMIN_GROUPS, ADMIN_SCRIPT, create_app
from clearscout.infrastructure.persistence.models import Base

APPEND_ONLY = "audit-event"
MUTABLE = "job-posting"


@pytest.fixture
def api(durable_database_url: str, tmp_path: Path):
    """The shipped application, addressed the way a browser would address it."""
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        yield client


def test_the_bare_admin_path_reaches_the_admin_and_not_the_application_shell(
    api: TestClient,
) -> None:
    """The SPA catch-all answers every unmatched path, and /admin is not one."""
    bare = api.get("/admin", follow_redirects=False)
    index = api.get("/admin/")

    assert bare.status_code == 307
    assert bare.headers["location"] == "/admin/"
    assert index.status_code == 200
    assert "<title>ClearScout admin</title>" in index.text
    # Served by the admin's own static mount, which the SPA build has no route for.
    assert "/admin/static/css/vendor/tabler.min.css" in index.text


def test_every_mapped_model_is_offered_as_an_admin_view(api: TestClient) -> None:
    """A model added without a view would silently be missing from the admin."""
    listed = set(re.findall(r"/admin/([a-z0-9-]+)/list", api.get("/admin/").text))

    assert len(listed) == len(list(Base.registry.mappers))
    assert {APPEND_ONLY, MUTABLE} <= listed


def test_neither_an_append_only_nor_a_mutable_table_offers_write_controls(
    api: TestClient,
) -> None:
    """Every view is read-only, so no list page invites a create."""
    append_only = api.get(f"/admin/{APPEND_ONLY}/list")
    mutable = api.get(f"/admin/{MUTABLE}/list")

    assert append_only.status_code == 200
    assert mutable.status_code == 200
    assert f"/admin/{APPEND_ONLY}/create" not in append_only.text
    assert f"/admin/{MUTABLE}/create" not in mutable.text


def _delete_action(api: TestClient, identity: str):
    """Ask the admin to delete a row, carrying the CSRF token a browser would."""
    api.get(f"/admin/{identity}/list")
    return api.post(
        f"/admin/_api/{identity}/action",
        params={"name": "delete", "pks": "absent"},
        headers={"X-CSRFToken": api.cookies["starlette_admin_csrftoken"]},
    )


def test_the_admin_refuses_to_create_edit_or_delete_any_row(
    api: TestClient,
) -> None:
    """Hiding the controls is not the boundary; refusing the request is."""
    assert api.get(f"/admin/{APPEND_ONLY}/create").status_code == 403
    assert api.get(f"/admin/{APPEND_ONLY}/edit", params={"pk": "absent"}).status_code == 403
    assert _delete_action(api, APPEND_ONLY).json() == {"msg": "Forbidden"}

    # A mutable table is refused the same way; the admin, not the trigger, says no.
    assert api.get(f"/admin/{MUTABLE}/create").status_code == 403
    assert api.get(f"/admin/{MUTABLE}/edit", params={"pk": "absent"}).status_code == 403
    assert _delete_action(api, MUTABLE).json() == {"msg": "Forbidden"}


def test_the_json_viewer_script_is_served_and_linked_from_a_model_page(api: TestClient) -> None:
    """The page's own inline call is CSP-blocked, so JSON renders only from this file."""
    asset = api.get(ADMIN_SCRIPT)

    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    assert "initJsonViewers()" in asset.text
    assert f'src="{ADMIN_SCRIPT}"' in api.get(f"/admin/{MUTABLE}/list").text


def test_the_menu_groups_the_models_instead_of_listing_all_of_them_flat(
    api: TestClient,
) -> None:
    """A flat list of every mapped model is not a navigable menu."""
    labels = re.findall(
        r'<span class="nav-link-title">\s*([^<]+?)\s*</span>', api.get("/admin/").text
    )

    assert labels == [label for _, label, _ in ADMIN_GROUPS]


def test_a_model_in_an_ungrouped_module_still_reaches_the_menu(
    monkeypatch: pytest.MonkeyPatch, durable_database_url: str, tmp_path: Path
) -> None:
    """A module nobody grouped must surface in the menu, not disappear from it."""
    monkeypatch.setattr(
        api_module,
        "ADMIN_GROUPS",
        tuple(group for group in ADMIN_GROUPS if group[0] != "artifacts"),
    )

    with TestClient(
        api_module.create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        index = client.get("/admin/").text
        labels = re.findall(r'<span class="nav-link-title">\s*([^<]+?)\s*</span>', index)

        assert labels[-1] == api_module.ADMIN_FALLBACK_LABEL
        assert "Audit and files" not in labels
        assert client.get(f"/admin/{APPEND_ONLY}/list").status_code == 200


def test_the_admin_never_creates_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "absent.sqlite3"

    with TestClient(
        create_app(f"sqlite+pysqlite:///{database_path}", tmp_path / "artifacts"),
        base_url="http://localhost",
        raise_server_exceptions=False,
    ) as client:
        index = client.get("/admin/")
        client.get(f"/admin/{MUTABLE}/list")
        client.get(f"/admin/{MUTABLE}/detail", params={"pk": "absent"})

    assert index.status_code == 200
    assert not database_path.exists()


def test_admin_responses_carry_the_local_browser_boundary_headers(api: TestClient) -> None:
    """A mounted sub-application must not escape the app-level header policy."""
    response = api.get("/admin/")

    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
