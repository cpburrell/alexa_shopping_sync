"""Service handlers for Alexa Shopping List Sync."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import AlexaShoppingCoordinator

_LOGGER = logging.getLogger(__name__)


def _target_coordinators(hass: HomeAssistant, call: ServiceCall) -> list[AlexaShoppingCoordinator]:
    """Return coordinators for a service call.

    Targets `config_entry_id` when given, otherwise every loaded entry.
    """
    wanted = call.data.get("config_entry_id")
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and wanted in (None, entry.entry_id)
    ]


async def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services.

    Registered once per domain, so handlers resolve their coordinators per call
    instead of capturing the entry that happened to set up first.
    """

    async def handle_force_refresh(call: ServiceCall) -> None:
        """Handle force_refresh service call."""
        _LOGGER.info("Service call: force_refresh")
        for coordinator in _target_coordinators(hass, call):
            await coordinator.async_force_refresh()

    async def handle_full_resync(call: ServiceCall) -> None:
        """Handle full_resync service call."""
        _LOGGER.info("Service call: full_resync")
        for coordinator in _target_coordinators(hass, call):
            result = await coordinator.async_full_resync()
            if result:
                _LOGGER.info(
                    "Full resync complete: Alexa->HA +%d ~%d -%d, HA->Alexa +%d ~%d -%d",
                    result.alexa_to_ha_adds,
                    result.alexa_to_ha_updates,
                    result.alexa_to_ha_deletes,
                    result.ha_to_alexa_adds,
                    result.ha_to_alexa_updates,
                    result.ha_to_alexa_deletes,
                )

    async def handle_clear_local_mapping(call: ServiceCall) -> None:
        """Handle clear_local_mapping service call."""
        _LOGGER.info("Service call: clear_local_mapping")
        for coordinator in _target_coordinators(hass, call):
            await coordinator.async_clear_local_mapping()

    async def handle_mark_reauth_needed(call: ServiceCall) -> None:
        """Handle mark_reauth_needed service call."""
        _LOGGER.info("Service call: mark_reauth_needed")
        for coordinator in _target_coordinators(hass, call):
            if coordinator.auth_manager:
                coordinator.auth_manager.mark_session_expired()
            coordinator._trigger_reauth()

    async def handle_export_diagnostics(call: ServiceCall) -> None:
        """Handle export_sanitized_diagnostics service call."""
        _LOGGER.info("Service call: export_sanitized_diagnostics")
        for coordinator in _target_coordinators(hass, call):
            _LOGGER.info("Diagnostics: %s", coordinator.get_diagnostics_data())

    if not hass.services.has_service(DOMAIN, "force_refresh"):
        hass.services.async_register(DOMAIN, "force_refresh", handle_force_refresh)
    if not hass.services.has_service(DOMAIN, "full_resync"):
        hass.services.async_register(DOMAIN, "full_resync", handle_full_resync)
    if not hass.services.has_service(DOMAIN, "clear_local_mapping"):
        hass.services.async_register(DOMAIN, "clear_local_mapping", handle_clear_local_mapping)
    if not hass.services.has_service(DOMAIN, "mark_reauth_needed"):
        hass.services.async_register(DOMAIN, "mark_reauth_needed", handle_mark_reauth_needed)
    if not hass.services.has_service(DOMAIN, "export_sanitized_diagnostics"):
        hass.services.async_register(
            DOMAIN, "export_sanitized_diagnostics", handle_export_diagnostics
        )
