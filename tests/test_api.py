from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from firestop.api.app import app, hydra
from firestop.hydra.client import HydraClient
from tests.conftest import QUERY_URL, page, value
from tests.paths import service_path

ADVISORY_HEADER = page(
    ["summary", "severity"],
    [[value("string", "Prototype pollution"), value("string", "high")]],
)

AFFECTED = page(
    ["key", "package", "version", "published_at", "fixed_in"],
    [
        [
            value("string", "evil@1.4.2"),
            value("string", "evil"),
            value("string", "1.4.2"),
            value("integer", 1_600_000_000),
            value("string", "1.4.3"),
        ]
    ],
)

SERVICES = page(
    ["name", "criticality"],
    [[value("string", "checkout-api"), value("string", "tier-1")]],
)

EXPOSED = page(["path"], [[service_path("checkout-api", package="evil", version="1.4.2")]])


@pytest.fixture
def api(client: HydraClient):
    app.dependency_overrides[hydra] = lambda: client
    with TestClient(app) as testing:
        yield testing
    app.dependency_overrides.clear()


@respx.mock
def test_blast_reports_the_exposed_services(api: TestClient):
    respx.post(QUERY_URL).mock(
        side_effect=[
            httpx.Response(200, json=ADVISORY_HEADER),
            httpx.Response(200, json=AFFECTED),
            httpx.Response(200, json=SERVICES),
            httpx.Response(200, json=EXPOSED),
        ]
    )

    body = api.get("/api/blast", params={"advisory": "GHSA-test"}).json()

    assert body["exposed"] == 1
    assert body["services"][0]["service"] == "checkout-api"
    assert body["compromise"]["severity"] == "high"


@respx.mock
def test_blast_carries_the_moment_it_answered_for(api: TestClient):
    respx.post(QUERY_URL).mock(
        side_effect=[
            httpx.Response(200, json=ADVISORY_HEADER),
            httpx.Response(200, json=AFFECTED),
            httpx.Response(200, json=SERVICES),
            httpx.Response(200, json=EXPOSED),
        ]
    )

    body = api.get("/api/blast", params={"advisory": "GHSA-test", "as_of": 1_700_000_000}).json()

    assert body["as_of"] == 1_700_000_000


@respx.mock
def test_an_open_ended_window_is_null_rather_than_a_sentinel(api: TestClient):
    respx.post(QUERY_URL).mock(
        side_effect=[
            httpx.Response(200, json=ADVISORY_HEADER),
            httpx.Response(200, json=AFFECTED),
            httpx.Response(200, json=SERVICES),
            httpx.Response(200, json=EXPOSED),
        ]
    )

    body = api.get("/api/blast", params={"advisory": "GHSA-test"}).json()

    assert body["services"][0]["paths"][0]["valid_to"] is None


@respx.mock
def test_an_unknown_advisory_is_a_404(api: TestClient):
    respx.post(QUERY_URL).respond(json=page(["summary", "severity"], []))

    response = api.get("/api/blast", params={"advisory": "GHSA-nope"})

    assert response.status_code == 404


def test_naming_nothing_is_a_400(api: TestClient):
    assert api.get("/api/blast").status_code == 400


@respx.mock
def test_the_landing_is_served(api: TestClient):
    response = api.get("/")

    assert response.status_code == 200
    assert "Firestop" in response.text
    assert "Map the blast radius" in response.text


@respx.mock
def test_the_console_is_served(api: TestClient):
    response = api.get("/console")

    assert response.status_code == 200
    assert "Firestop" in response.text
    assert 'id="stage-blast"' in response.text
    assert api.get("/static/styles.css").status_code == 200
    assert api.get("/static/theme.css").status_code == 200
    assert api.get("/static/app.js").status_code == 200
    assert api.get("/static/layout.js").status_code == 200
    assert api.get("/static/fonts/Syne-Variable.ttf").status_code == 200


@respx.mock
def test_explainer_pages_are_served(api: TestClient):
    for path in ("/how-it-works", "/features", "/architecture"):
        response = api.get(path)
        assert response.status_code == 200, path
        assert "Firestop" in response.text


@respx.mock
def test_overview_lists_what_the_console_needs(api: TestClient):
    respx.post(QUERY_URL).mock(
        side_effect=[
            httpx.Response(200, json=SERVICES),
            httpx.Response(
                200,
                json=page(
                    ["name", "dependents"],
                    [[value("string", "lodash"), value("integer", 900)]],
                ),
            ),
            httpx.Response(
                200,
                json=page(
                    ["osv_id", "summary", "severity", "published_at"],
                    [
                        [
                            value("string", "GHSA-test"),
                            value("string", "Prototype pollution"),
                            value("string", "high"),
                            value("integer", 1_600_000_000),
                        ]
                    ],
                ),
            ),
        ]
    )

    body = api.get("/api/overview").json()

    assert body["services"][0]["name"] == "checkout-api"
    assert body["packages"][0]["name"] == "lodash"
    assert body["advisories"][0]["osv_id"] == "GHSA-test"
