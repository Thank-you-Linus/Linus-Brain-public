"""
Tests for the Linus Brain config flow, including the trust contract consent.

Covers the installation journey:
- The connection check leads to the consent step (no entry created before it)
- An already configured Supabase URL aborts with already_configured
- The consent must be explicitly ticked, then is stored versioned and dated
- The contract link follows the user's language: French page for French,
  English page for every other language
- The options flow never touches the stored consent
- Import is not supported, and existing entries without consent still load
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from freezegun import freeze_time
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import async_setup_entry
from .. import coordinator as coordinator_module
from ..config_flow import LinusBrainConfigFlow, _build_options
from ..const import (
    CONF_CONTRACT_ACCEPTED,
    CONF_CONTRACT_ACCEPTED_AT,
    CONF_CONTRACT_VERSION,
    CONF_INACTIVE_TIMEOUT,
    CONF_SUPABASE_KEY,
    CONF_SUPABASE_URL,
    CONTRACT_SITE,
    CONTRACT_URL_EN,
    CONTRACT_URL_FR,
    CONTRACT_VERSION,
    DOMAIN,
)

URL = "https://example.supabase.co"
KEY = "fake-key"
FROZEN_NOW = "2026-09-30T10:00:00+00:00"

USER_INPUT = {CONF_URL: URL, CONF_API_KEY: KEY, CONF_INACTIVE_TIMEOUT: 90}

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def expose_repo_integration(enable_custom_integrations):
    """
    Make Home Assistant's loader find this repository's integration.

    pytest-homeassistant-custom-component ships its own `custom_components`
    package, which shadows the repository one; its path is extended so that
    `linus_brain` is discovered and the real flow handler is used.
    """
    import custom_components

    root = str(REPO_ROOT / "custom_components")
    if root not in custom_components.__path__:
        custom_components.__path__.append(root)


def _mock_session(status: int | None = None, error: Exception | None = None):
    """
    Build a fake aiohttp session for validate_supabase_connection.

    Args:
        status: HTTP status returned by session.get
        error: Exception raised by session.get instead of answering

    Returns:
        A MagicMock usable as the result of async_get_clientsession
    """
    session = MagicMock()
    if error is not None:
        session.get.side_effect = error
    else:
        response = MagicMock()
        response.status = status
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        session.get.return_value = context
    return session


async def _start_user_step(hass, user_input=USER_INPUT):
    """Start the flow and submit the user step with a validated connection."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.linus_brain.config_flow.validate_supabase_connection",
        AsyncMock(return_value={"status": "ok"}),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )


class TestConnectionValidation:
    """The connection check leads to consent or to an error."""

    @pytest.mark.parametrize("status", [200, 401, 404])
    async def test_reachable_statuses_lead_to_consent(
        self, hass, enable_custom_integrations, status
    ):
        """200, 401 and 404 validate the connection: no entry before consent."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        with patch(
            "custom_components.linus_brain.config_flow.async_get_clientsession",
            return_value=_mock_session(status=status),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "consent"
        assert hass.config_entries.async_entries(DOMAIN) == []

    @pytest.mark.parametrize(
        "session",
        [
            _mock_session(status=500),
            _mock_session(error=aiohttp.ClientError("boom")),
        ],
        ids=["status_500", "client_error"],
    )
    async def test_unreachable_shows_cannot_connect(
        self, hass, enable_custom_integrations, session
    ):
        """A 500 or a network error keeps the user step with cannot_connect."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        with patch(
            "custom_components.linus_brain.config_flow.async_get_clientsession",
            return_value=session,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "cannot_connect"}
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_already_configured_aborts(self, hass, enable_custom_integrations):
        """The same Supabase URL aborts with already_configured (not cannot_connect)."""
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=f"{DOMAIN}_{URL}",
            data={CONF_SUPABASE_URL: URL, CONF_SUPABASE_KEY: KEY},
        ).add_to_hass(hass)

        result = await _start_user_step(hass)

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestConsentStep:
    """The explicit, versioned consent."""

    async def test_unticked_box_is_refused(self, hass, enable_custom_integrations):
        """An unticked box re-shows the consent form with consent_required."""
        result = await _start_user_step(hass)
        assert result["step_id"] == "consent"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"accept_contract": False}
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "consent"
        assert result["errors"] == {"base": "consent_required"}
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_consent_form_exposes_contract_placeholders(
        self, hass, enable_custom_integrations
    ):
        """The form carries the contract site and version as data only."""
        result = await _start_user_step(hass)

        assert result["description_placeholders"] == {
            "contract_site": CONTRACT_SITE,
            "contract_version": CONTRACT_VERSION,
        }

    async def test_ticked_box_creates_versioned_entry(
        self, hass, enable_custom_integrations
    ):
        """A ticked box creates the entry with the dated, versioned consent."""
        result = await _start_user_step(hass)

        with (
            freeze_time(FROZEN_NOW),
            patch("custom_components.linus_brain.async_setup_entry", return_value=True),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"accept_contract": True}
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Linus Brain"
        assert result["data"] == {
            CONF_SUPABASE_URL: URL,
            CONF_SUPABASE_KEY: KEY,
            CONF_CONTRACT_ACCEPTED: True,
            CONF_CONTRACT_VERSION: "1",
            CONF_CONTRACT_ACCEPTED_AT: FROZEN_NOW,
        }
        assert result["options"] == _build_options(USER_INPUT)
        assert result["options"][CONF_INACTIVE_TIMEOUT] == 90

    async def test_options_flow_keeps_consent(self, hass, enable_custom_integrations):
        """Submitting the options flow leaves the consent keys of data intact."""
        result = await _start_user_step(hass)
        with (
            freeze_time(FROZEN_NOW),
            patch("custom_components.linus_brain.async_setup_entry", return_value=True),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"accept_contract": True}
            )
            await hass.async_block_till_done()
        entry = result["result"]

        options_flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert options_flow["type"] is FlowResultType.FORM
        with patch(
            "custom_components.linus_brain.async_setup_entry", return_value=True
        ):
            options_result = await hass.config_entries.options.async_configure(
                options_flow["flow_id"], {CONF_INACTIVE_TIMEOUT: 120}
            )
            await hass.async_block_till_done()

        assert options_result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_INACTIVE_TIMEOUT] == 120
        assert entry.data[CONF_CONTRACT_ACCEPTED] is True
        assert entry.data[CONF_CONTRACT_VERSION] == "1"
        assert entry.data[CONF_CONTRACT_ACCEPTED_AT] == FROZEN_NOW

    async def test_consent_without_user_step_does_not_crash(
        self, hass, enable_custom_integrations
    ):
        """Calling the consent step directly (no user step) does not raise."""
        flow = LinusBrainConfigFlow()
        flow.hass = hass

        result = await flow.async_step_consent()

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "consent"


class TestContractLinkByLanguage:
    """
    The consent step links the contract page in the user's language.

    The frontend asks for the translations of the user's profile language, and
    Home Assistant fills every missing key from English. fr.json links the
    French page, en.json the English one: any language other than French falls
    back to the English text, hence to the English page. Home Assistant also
    drops a translation whose placeholders differ from English: these tests
    would catch the French text silently replaced by the English one.
    """

    async def _rendered_description(self, hass, language: str) -> str:
        """Render the consent description as a user in `language` sees it."""
        result = await _start_user_step(hass)
        translations = await async_get_translations(hass, language, "config", [DOMAIN])
        description = translations[
            f"component.{DOMAIN}.config.step.consent.description"
        ]
        for name, value in result["description_placeholders"].items():
            description = description.replace(f"{{{name}}}", value)
        return description

    def test_contract_urls(self):
        """Both pages of the contract are the addresses decided for version 1."""
        assert CONTRACT_URL_FR == (
            "https://thankyou-linus.com/contrat-de-confiance-linus-brain/"
        )
        assert CONTRACT_URL_EN == (
            "https://thankyou-linus.com/en/linus-brain-trust-contract/"
        )

    async def test_french_user_gets_french_page(self, hass, enable_custom_integrations):
        """A user in French is sent to the French page only."""
        description = await self._rendered_description(hass, "fr")

        assert f"[{CONTRACT_URL_FR}]({CONTRACT_URL_FR})" in description
        assert CONTRACT_URL_EN not in description

    @pytest.mark.parametrize("language", ["en", "de", "es", "nl"])
    async def test_other_languages_get_english_page(
        self, hass, enable_custom_integrations, language
    ):
        """Any language other than French is sent to the English page only."""
        description = await self._rendered_description(hass, language)

        assert f"[{CONTRACT_URL_EN}]({CONTRACT_URL_EN})" in description
        assert CONTRACT_URL_FR not in description


class TestImportAndMigration:
    """Import is refused; existing entries are untouched."""

    async def test_import_is_not_supported(self, hass, enable_custom_integrations):
        """Import aborts with import_not_supported and creates nothing."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=USER_INPUT,
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "import_not_supported"
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_existing_entry_without_consent_loads(
        self, hass, enable_custom_integrations
    ):
        """An entry created before the consent still sets up, with no new flow."""
        assert LinusBrainConfigFlow.VERSION == 1

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_SUPABASE_URL: URL, CONF_SUPABASE_KEY: KEY},
            options={},
        )
        entry.add_to_hass(hass)
        hass.data["core.uuid"] = "ha-installation-uuid-0001"
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

        client = AsyncMock()
        client.get_instance_by_ha_id.return_value = None
        client.create_new_instance.return_value = None
        client.update_instance_last_seen.return_value = None
        client.fetch_area_insights.return_value = None
        client.fetch_activity_types.return_value = None
        client.fetch_app_with_actions.return_value = None

        with (
            patch.object(coordinator_module, "SupabaseClient", return_value=client),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(return_value=True),
            ),
        ):
            assert await async_setup_entry(hass, entry) is True

        assert hass.config_entries.flow.async_progress() == []
