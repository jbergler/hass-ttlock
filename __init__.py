"""The TTLock integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .api import TTLockApi
from .const import DOMAIN, TT_API, TT_LOCKS
from .coordinator import LockUpdateCoordinator
from .services import Services
from .webhook import WebhookHandler

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

# Tiempo de espera para no bloquear el inicio de Home Assistant indefinidamente.
_LOCK_LIST_TIMEOUT = 15  # segundos


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up TTLock (YAML)."""
    # Usamos async_setup para registrar servicios de manera global
    Services(hass).register()
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TTLock from a config entry."""
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    client = TTLockApi(aiohttp_client.async_get_clientsession(hass), session)

    # Inicializamos la estructura de datos
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        TT_API: client,
        TT_LOCKS: []
    }

    # Descubrir cerraduras con un timeout estricto
    try:
        lock_ids = await asyncio.wait_for(client.get_locks(), timeout=_LOCK_LIST_TIMEOUT)
    except (asyncio.TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(
            f"Timeout/unavailable while fetching TTLock lock list ({_LOCK_LIST_TIMEOUT}s)"
        ) from err
    except Exception as err:
        _LOGGER.error("TTLock not ready: %s", err)
        raise ConfigEntryNotReady("TTLock not ready while fetching lock list") from err

    # Crear coordinadores para cada cerradura encontrada
    coordinators = [
        LockUpdateCoordinator(hass, entry, client, lock_id) for lock_id in lock_ids
    ]
    
    # IMPORTANTE: Guardar coordinadores antes de cargar plataformas
    hass.data[DOMAIN][entry.entry_id][TT_LOCKS] = coordinators

    # Cargar las plataformas (lock, sensor, etc.)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Configuración del Webhook en segundo plano para no bloquear el arranque
    async def _setup_webhook() -> None:
        try:
            await WebhookHandler(hass, entry).setup()
        except Exception:
            _LOGGER.exception("Failed to set up TTLock webhook")

    entry.async_create_background_task(
        hass, _setup_webhook(), f"ttlock_webhook_{entry.entry_id}"
    )

    # Lanzar el primer refresh de datos en segundo plano
    for coordinator in coordinators:
        entry.async_create_background_task(
            hass,
            coordinator.async_config_entry_first_refresh(),
            f"ttlock_first_refresh_{entry.entry_id}_{coordinator.lock_id}"
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        
        # Si no hay más entradas, limpiamos el dominio
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
            
    return unload_ok