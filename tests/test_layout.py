# pyright: reportMissingImports=false
"""Tests for the Layout meta-plugin (src/plugins/layout).

Child plugins are always faked here — never call real Dexcom/Nutrislice/
Weather/Calendar APIs from this file.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image


class FakeDeviceConfig:
    """Minimal DeviceConfigLike + get_plugin()/plugin_image_dir double."""

    def __init__(
        self,
        tmp_path: Path,
        plugins: dict[str, dict[str, str]],
        resolution: tuple[int, int] = (800, 480),
        orientation: str = "horizontal",
    ) -> None:
        self._resolution = resolution
        self._orientation = orientation
        self._plugins = plugins
        self.plugin_image_dir = str(tmp_path / "plugin_images")

    def get_resolution(self) -> tuple[int, int]:
        return self._resolution

    def get_config(self, key: str | None = None, default: Any = None) -> Any:
        if key == "orientation":
            return self._orientation
        return default

    def load_env_key(self, key: str) -> str | None:
        return None

    def get_plugin(self, plugin_id: str) -> dict[str, str] | None:
        return self._plugins.get(plugin_id)


class FakeColorPlugin:
    """Fake child plugin: fills its assigned region with a solid color.

    Tracks call count (class-level, keyed by plugin id) so tests can assert
    on caching/reuse behavior without touching a real upstream API.
    """

    call_counts: dict[str, int] = {}
    fail_ids: set[str] = set()

    def __init__(self, config: dict[str, str]) -> None:
        self.config = config

    def generate_image(
        self, settings: dict[str, Any], device_config: Any
    ) -> Image.Image:
        plugin_id = self.config["id"]
        FakeColorPlugin.call_counts[plugin_id] = (
            FakeColorPlugin.call_counts.get(plugin_id, 0) + 1
        )
        if plugin_id in FakeColorPlugin.fail_ids:
            raise RuntimeError(f"fake upstream failure for {plugin_id}")
        w, h = device_config.get_resolution()
        color = settings.get("color", "red")
        return Image.new("RGB", (w, h), color)


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def _reset_fake_plugin_state() -> Iterator[None]:
    FakeColorPlugin.call_counts = {}
    FakeColorPlugin.fail_ids = set()
    yield


@pytest.fixture()  # type: ignore[untyped-decorator]
def plugin() -> Any:
    from plugins.layout.layout import Layout

    return Layout({"id": "layout", "class": "Layout", "name": "Layout"})


def _patch_get_plugin_instance() -> Any:
    return patch(
        "plugins.layout.layout.get_plugin_instance",
        side_effect=lambda config: FakeColorPlugin(config),
    )


def _plugins_map(*ids: str) -> dict[str, dict[str, str]]:
    return {pid: {"id": pid, "class": "FakeColorPlugin"} for pid in ids}


# ---------------------------------------------------------------------------
# Regions land at the correct pixel offsets
# ---------------------------------------------------------------------------


def test_regions_land_at_correct_pixel_offsets(plugin: Any, tmp_path: Path) -> None:
    regions = [
        {
            "plugin_id": "fake_a",
            "x": 0,
            "y": 0,
            "w": 400,
            "h": 200,
            "settings": {"color": "red"},
        },
        {
            "plugin_id": "fake_b",
            "x": 400,
            "y": 0,
            "w": 400,
            "h": 200,
            "settings": {"color": "blue"},
        },
        {
            "plugin_id": "fake_a",
            "x": 0,
            "y": 200,
            "w": 800,
            "h": 280,
            "settings": {"color": "green"},
        },
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a", "fake_b"))
    settings = {"regionsJson": json.dumps(regions)}

    with _patch_get_plugin_instance():
        image = plugin.generate_image(settings, device_config)

    assert image.size == (800, 480)
    # Sample well inside each region (avoiding the 1px black border) to
    # confirm the right child's output landed at the right offset.
    assert image.getpixel((200, 100)) == (255, 0, 0)  # region 1 (red)
    assert image.getpixel((600, 100)) == (0, 0, 255)  # region 2 (blue)
    assert image.getpixel((400, 350)) == (0, 128, 0)  # region 3 (green)


def test_region_border_is_drawn(plugin: Any, tmp_path: Path) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 10, "y": 10, "w": 100, "h": 100, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with _patch_get_plugin_instance():
        image = plugin.generate_image(settings, device_config)

    assert image.getpixel((10, 10)) == (0, 0, 0)
    assert image.getpixel((109, 10)) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Out-of-bounds validation
# ---------------------------------------------------------------------------


def test_generate_image_rejects_out_of_bounds_region(
    plugin: Any, tmp_path: Path
) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 700, "y": 0, "w": 200, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with pytest.raises(RuntimeError, match="exceeds the"):
        plugin.generate_image(settings, device_config)


def test_generate_image_rejects_negative_offset(plugin: Any, tmp_path: Path) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": -5, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with pytest.raises(RuntimeError, match="negative"):
        plugin.generate_image(settings, device_config)


def test_generate_image_requires_at_least_one_region(
    plugin: Any, tmp_path: Path
) -> None:
    device_config = FakeDeviceConfig(tmp_path, {})
    with pytest.raises(RuntimeError, match="At least one region"):
        plugin.generate_image({"regionsJson": "[]"}, device_config)


def test_generate_image_rejects_unknown_plugin(plugin: Any, tmp_path: Path) -> None:
    regions = [
        {
            "plugin_id": "does_not_exist",
            "x": 0,
            "y": 0,
            "w": 100,
            "h": 60,
            "settings": {},
        }
    ]
    device_config = FakeDeviceConfig(tmp_path, {})
    settings = {"regionsJson": json.dumps(regions)}

    with pytest.raises(RuntimeError, match="not installed"):
        plugin.generate_image(settings, device_config)


def test_generate_image_rejects_self_reference(plugin: Any, tmp_path: Path) -> None:
    regions = [
        {"plugin_id": "layout", "x": 0, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, {})
    settings = {"regionsJson": json.dumps(regions)}

    with pytest.raises(RuntimeError, match="itself"):
        plugin.generate_image(settings, device_config)


def test_validate_settings_rejects_out_of_bounds_region(plugin: Any) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 700, "y": 0, "w": 200, "h": 60, "settings": {}}
    ]
    error = plugin.validate_settings({"regionsJson": json.dumps(regions)})
    assert error is not None
    assert "exceeds the" in error


def test_validate_settings_accepts_valid_regions(plugin: Any) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 800, "h": 60, "settings": {}}
    ]
    assert plugin.validate_settings({"regionsJson": json.dumps(regions)}) is None


def test_validate_settings_rejects_invalid_json(plugin: Any) -> None:
    error = plugin.validate_settings({"regionsJson": "not json"})
    assert error is not None


# ---------------------------------------------------------------------------
# Per-region caching
# ---------------------------------------------------------------------------


def test_cached_region_skips_child_plugin_within_refresh_window(
    plugin: Any, tmp_path: Path
) -> None:
    regions = [
        {
            "plugin_id": "fake_a",
            "x": 0,
            "y": 0,
            "w": 100,
            "h": 60,
            "settings": {},
            "refresh_minutes": 15,
        }
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with _patch_get_plugin_instance():
        plugin.generate_image(settings, device_config)
        plugin.generate_image(settings, device_config)

    assert FakeColorPlugin.call_counts["fake_a"] == 1


def test_region_without_refresh_minutes_always_refreshes(
    plugin: Any, tmp_path: Path
) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with _patch_get_plugin_instance():
        plugin.generate_image(settings, device_config)
        plugin.generate_image(settings, device_config)

    assert FakeColorPlugin.call_counts["fake_a"] == 2


def test_child_failure_falls_back_to_stale_cache_instead_of_raising(
    plugin: Any, tmp_path: Path
) -> None:
    regions = [
        {
            "plugin_id": "fake_a",
            "x": 0,
            "y": 0,
            "w": 100,
            "h": 60,
            "settings": {"color": "red"},
            "refresh_minutes": 0,
        }
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with _patch_get_plugin_instance():
        first = plugin.generate_image(settings, device_config)
        FakeColorPlugin.fail_ids.add("fake_a")
        second = plugin.generate_image(settings, device_config)

    # Region content (excluding the border) should be identical: the stale
    # cached render was reused rather than a blank error placeholder.
    assert first.getpixel((50, 30)) == second.getpixel((50, 30)) == (255, 0, 0)


def test_child_failure_without_cache_renders_placeholder_not_raise(
    plugin: Any, tmp_path: Path
) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}
    FakeColorPlugin.fail_ids.add("fake_a")

    with _patch_get_plugin_instance():
        image = plugin.generate_image(settings, device_config)

    assert image.size == (800, 480)


# ---------------------------------------------------------------------------
# Resizing a mismatched child image
# ---------------------------------------------------------------------------


def test_mismatched_child_image_size_gets_resized(plugin: Any, tmp_path: Path) -> None:
    class WrongSizePlugin:
        def __init__(self, config: dict[str, str]) -> None:
            self.config = config

        def generate_image(
            self, settings: dict[str, Any], device_config: Any
        ) -> Image.Image:
            return Image.new("RGB", (10, 10), "purple")

    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with patch(
        "plugins.layout.layout.get_plugin_instance",
        side_effect=lambda config: WrongSizePlugin(config),
    ):
        image = plugin.generate_image(settings, device_config)

    assert image.size == (800, 480)
    assert image.getpixel((50, 30)) == (128, 0, 128)


# ---------------------------------------------------------------------------
# Settings template / available plugins listing
# ---------------------------------------------------------------------------


def test_generate_settings_template_lists_available_plugins_and_presets(
    plugin: Any,
) -> None:
    template_params = plugin.generate_settings_template()
    assert "available_plugins" in template_params
    ids = {p["id"] for p in template_params["available_plugins"]}
    # "layout" must not offer itself as a region target.
    assert "layout" not in ids
    assert "presets_json" in template_params
    presets = json.loads(template_params["presets_json"])
    assert "home" in presets
    assert len(presets["home"]) == 4
