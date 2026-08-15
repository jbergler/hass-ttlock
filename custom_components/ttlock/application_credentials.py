"""application_credentials platform the TTLock integration."""

from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant

from .api import TTLockAuthImplementation
from .const import CONF_REGION, DEFAULT_REGION, DOMAIN, REGIONS


def _region_for_auth_domain(hass: HomeAssistant, auth_domain: str) -> str:
    """Recover the region a stored credential belongs to.

    application_credentials rebuilds the implementation from the credential
    alone - it passes no config entry and no region - so region is read back
    from the entry that uses this credential (region is 1:1 with a TTLock
    developer app / clientId). Entries created before region support, and
    credentials with no entry yet (mid config-flow), default to EU.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("auth_implementation") == auth_domain:
            return entry.data.get(CONF_REGION, DEFAULT_REGION)
    return DEFAULT_REGION


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> TTLockAuthImplementation:
    """Return custom auth implementation."""
    region = _region_for_auth_domain(hass, auth_domain)
    return TTLockAuthImplementation(
        hass,
        auth_domain,
        credential,
        AuthorizationServer(
            authorize_url="",
            token_url=REGIONS[region]["token_url"],
        ),
    )
