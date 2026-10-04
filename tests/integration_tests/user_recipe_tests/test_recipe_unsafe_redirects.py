"""
Fork: safehttp refuses a redirect from https to plain http, or off http(s) (mealie/pkgs/safehttp/redirects.py). The
host was allowed, so these routes say what happened instead of "Url is not from an allowed domain" or a generic error.
"""

import pytest
from fastapi.testclient import TestClient

from mealie.lang import get_locale_provider
from mealie.pkgs import safehttp
from mealie.schema.recipe.recipe import Recipe
from mealie.services.recipe import recipe_data_service
from mealie.services.scraper import recipe_scraper
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

translator = get_locale_provider("en-US")

REFUSALS = [
    pytest.param(
        safehttp.UnsafeRedirectError("refusing a downgrade", downgrade=True),
        "Url redirected to an insecure http:// address",
        "recipe.import-errors.insecure-redirect",
        id="https-to-http",
    ),
    pytest.param(
        safehttp.UnsafeRedirectError("refusing a file: URL"),
        "Url redirected to an address that isn't http or https",
        "recipe.import-errors.unsafe-redirect",
        id="off-http",
    ),
]


def _refuse(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def fake_fetch(*args, **kwargs):
        raise error

    monkeypatch.setattr(recipe_data_service.safehttp, "resilient_fetch", fake_fetch)


def _message(response) -> str:
    return response.json()["detail"]["message"]


@pytest.mark.parametrize(("error", "route_message", "import_key"), REFUSALS)
def test_image_url_reports_a_refused_redirect(
    api_client: TestClient,
    unique_user: TestUser,
    recipe_ingredient_only: Recipe,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    route_message: str,
    import_key: str,
):
    _refuse(monkeypatch, error)
    response = api_client.post(
        api_routes.recipes_slug_image(recipe_ingredient_only.slug),
        json={"url": "https://example.test/pancakes.jpg"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert _message(response) == route_message


@pytest.mark.parametrize(("error", "route_message", "import_key"), REFUSALS)
def test_asset_url_reports_a_refused_redirect(
    api_client: TestClient,
    unique_user: TestUser,
    recipe_ingredient_only: Recipe,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    route_message: str,
    import_key: str,
):
    _refuse(monkeypatch, error)
    response = api_client.post(
        api_routes.recipes_slug_assets_url(recipe_ingredient_only.slug),
        json={"url": "https://example.test/pancakes.png"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert _message(response) == route_message
    assert not any(recipe_ingredient_only.asset_dir.iterdir())


def test_a_blocked_domain_keeps_its_message(
    api_client: TestClient, unique_user: TestUser, recipe_ingredient_only: Recipe, monkeypatch: pytest.MonkeyPatch
):
    _refuse(monkeypatch, safehttp.InvalidDomainError("private address"))
    response = api_client.post(
        api_routes.recipes_slug_image(recipe_ingredient_only.slug),
        json={"url": "https://example.test/pancakes.jpg"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert _message(response) == "Url is not from an allowed domain"


@pytest.mark.parametrize(("error", "route_message", "import_key"), REFUSALS)
def test_url_import_reports_a_refused_redirect(
    api_client: TestClient,
    unique_user: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    route_message: str,
    import_key: str,
):
    async def fake_scrape(url: str) -> str:
        raise error

    monkeypatch.setattr(recipe_scraper, "safe_scrape_html", fake_scrape)
    response = api_client.post(
        api_routes.recipes_create_url, json={"url": "https://example.test/recipe"}, headers=unique_user.token
    )
    assert response.status_code == 400
    assert _message(response) == translator.t(import_key)
    assert _message(response) != translator.t("recipe.import-errors.unknown-error")


def test_image_url_reports_a_body_over_the_cap(
    api_client: TestClient, unique_user: TestUser, recipe_ingredient_only: Recipe, monkeypatch: pytest.MonkeyPatch
):
    """Fork: safehttp caps every body (DEFAULT_MAX_BYTES); an image over it is a 400, not a server error"""
    _refuse(monkeypatch, safehttp.ResponseTooLargeError("response body exceeds its cap"))
    response = api_client.post(
        api_routes.recipes_slug_image(recipe_ingredient_only.slug),
        json={"url": "https://example.test/huge.jpg"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert _message(response) == f"Image is larger than {safehttp.DEFAULT_MAX_BYTES // (1024 * 1024)}MB"
