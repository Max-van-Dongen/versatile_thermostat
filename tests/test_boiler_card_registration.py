"""The optional dashboard card is loaded once when the frontend becomes available."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.const import EVENT_COMPONENT_LOADED

from custom_components import versatile_thermostat


@pytest.mark.parametrize("frontend_ready", [False, True])
async def test_boiler_card_loads_from_the_integration(hass, frontend_ready):
    """Support headless startup, late frontend loading and installation on a running UI."""
    if frontend_ready:
        hass.config.components.add("frontend")
    on_change = Mock()
    modules = hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(on_change, [])
    register_path = AsyncMock()
    http = SimpleNamespace(async_register_static_paths=register_path)
    with (
        patch.object(hass, "http", http, create=True),
        patch.object(versatile_thermostat, "async_register_log_download_endpoint"),
        patch.object(versatile_thermostat, "async_get_integration", return_value=SimpleNamespace(version="test-version")),
    ):
        assert await versatile_thermostat.async_setup(hass, {})
        await hass.async_block_till_done()
        if not frontend_ready:
            register_path.assert_not_awaited()
            assert not modules.urls
            hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "frontend"})
            await hass.async_block_till_done()

        # An additional component-loaded event must not register a second route.
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "frontend"})
        await hass.async_block_till_done()

    register_path.assert_awaited_once()
    (static_path,) = register_path.await_args.args[0]
    route = "/versatile_thermostat/boiler-pulse-card.js"
    assert static_path.url_path == route
    asset = Path(static_path.path)
    assert asset == Path(versatile_thermostat.__file__).with_name("www") / "boiler-pulse-card.js"
    assert asset.is_file()
    assert modules.urls == {f"{route}?v=test-version"}
    on_change.assert_called_once_with("added", f"{route}?v=test-version")
