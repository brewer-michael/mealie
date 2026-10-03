from uuid import uuid4

import pytest
from pydantic import ValidationError

from mealie.schema.group.ai_providers import (
    AIProviderCreate,
    AIProviderOut,
    AIProviderProtocol,
    AIProviderSlot,
    AIProviderSummary,
    AIProviderUpdate,
)
from mealie.schema.group.ai_routing import AIProviderModelsQuery, AIProviderRoutesUpdate


def test_protocol_defaults_to_openai():
    provider = AIProviderCreate(name="test", api_key="key", model="gpt-4o")
    assert provider.protocol is AIProviderProtocol.openai
    assert provider.monthly_token_limit is None


def test_protocol_accepts_anthropic_and_rejects_unknown():
    assert AIProviderCreate(name="t", api_key="k", model="m", protocol="anthropic").protocol == "anthropic"

    with pytest.raises(ValidationError):
        AIProviderCreate(name="t", api_key="k", model="m", protocol="gemini")


@pytest.mark.parametrize("limit", [1, 1_000_000])
def test_monthly_token_limit_valid(limit: int):
    assert AIProviderCreate(name="t", api_key="k", model="m", monthly_token_limit=limit).monthly_token_limit == limit


@pytest.mark.parametrize("limit", [0, -1])
def test_monthly_token_limit_must_be_positive(limit: int):
    with pytest.raises(ValidationError):
        AIProviderCreate(name="t", api_key="k", model="m", monthly_token_limit=limit)


@pytest.mark.parametrize("limit", ["", None])
def test_monthly_token_limit_blank_is_unset(limit: str | None):
    assert AIProviderCreate(name="t", api_key="k", model="m", monthly_token_limit=limit).monthly_token_limit is None


def test_camel_case_input():
    provider = AIProviderCreate.model_validate(
        {"name": "t", "apiKey": "k", "model": "m", "protocol": "anthropic", "monthlyTokenLimit": 5}
    )
    assert (provider.protocol, provider.monthly_token_limit, provider.api_key) == ("anthropic", 5, "k")


def test_provider_out_loads_with_an_unreadable_key():
    """A stored key that can't be decrypted reads as "", and the provider must still load"""
    provider = AIProviderOut(id=uuid4(), name="t", api_key="", model="m")
    assert provider.api_key == ""

    for field in ["name", "model"]:
        with pytest.raises(ValidationError):
            AIProviderOut(**{"id": uuid4(), "name": "t", "api_key": "k", "model": "m", field: ""})

    # Creating or updating still requires a key when one is given
    with pytest.raises(ValidationError):
        AIProviderCreate(name="t", api_key="", model="m")


def test_routes_update_validates_slots():
    provider_id = uuid4()
    update = AIProviderRoutesUpdate.model_validate({"routes": {"fast": [str(provider_id)]}})
    assert update.routes == {AIProviderSlot.fast: [provider_id]}

    with pytest.raises(ValidationError):
        AIProviderRoutesUpdate.model_validate({"routes": {"nope": [str(provider_id)]}})


def test_models_query_needs_no_name_or_model_and_hides_the_key():
    query = AIProviderModelsQuery.model_validate({"apiKey": "secret", "baseUrl": ""})
    assert query.api_key == "secret"
    assert query.base_url is None
    assert query.protocol is AIProviderProtocol.openai
    assert "secret" not in query.model_dump_json()


def test_monthly_token_limit_fits_a_32_bit_integer_column():
    limit = 2_147_483_647
    assert AIProviderCreate(name="t", api_key="k", model="m", monthly_token_limit=limit).monthly_token_limit == limit

    with pytest.raises(ValidationError):
        AIProviderCreate(name="t", api_key="k", model="m", monthly_token_limit=limit + 1)


@pytest.mark.parametrize(
    "base_url", ["https://internal.test/v1?", "https://internal.test/v1?x=1", "https://internal.test/v1#frag"]
)
def test_base_url_rejects_a_query_string_or_fragment(base_url: str):
    # Paths like /models are appended to the base URL; a "?" would turn them into a query value
    with pytest.raises(ValidationError, match="query string or fragment"):
        AIProviderCreate(name="t", api_key="k", model="m", base_url=base_url)
    with pytest.raises(ValidationError, match="query string or fragment"):
        AIProviderUpdate(name="t", api_key="k", model="m", base_url=base_url)
    with pytest.raises(ValidationError, match="query string or fragment"):
        AIProviderModelsQuery(api_key="k", base_url=base_url)

    # A provider saved before the check still loads
    assert AIProviderOut(id=uuid4(), name="t", api_key="k", model="m", base_url=base_url).base_url == base_url


def test_base_url_accepts_plain_urls():
    base_url = "http://ollama.local:11434/v1/"
    assert AIProviderCreate(name="t", api_key="k", model="m", base_url=base_url).base_url == base_url
    assert AIProviderModelsQuery(api_key="k", base_url=base_url).base_url == base_url


def test_api_key_set_never_exposes_the_key():
    provider = AIProviderOut(id=uuid4(), name="t", api_key="sk-secret", model="m")
    assert provider.api_key_set is True
    dumped = provider.model_dump_json(by_alias=True)
    assert '"apiKeySet":true' in dumped
    assert "sk-secret" not in dumped

    # An undecryptable key reads as ""
    assert AIProviderOut(id=uuid4(), name="t", api_key="", model="m").api_key_set is False


def test_provider_summary_has_no_key_data():
    """The summary is in `/groups/self`, which every group member can read"""
    assert set(AIProviderSummary(id=uuid4(), name="t").model_dump(by_alias=True)) == {"id", "name"}
