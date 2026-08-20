# pyright: reportMissingImports=false
"""Tests for the Layout meta-plugin (src/plugins/layout).

Child plugins are always faked here — never call real Dexcom/Nutrislice/
Weather/Calendar APIs from this file.
"""

import copy
import json
import os
import time
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


def _as_subprocess_would(settings: dict[str, Any]) -> dict[str, Any]:
    """Return the settings dict as a plugin subprocess actually receives it.

    InkyPi runs ``generate_image`` in a separate process by default
    (``INKYPI_PLUGIN_ISOLATION=process``), so the plugin is handed a *pickled
    copy* of the instance's settings and anything it writes back into that
    dict dies with the child.  Tests that pass one long-lived dict across
    several ``generate_image`` calls silently grant Layout a persistence
    channel production never gives it — deep-copying per call is what the
    real refresh path does.
    """
    return copy.deepcopy(settings)


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


def test_validate_settings_rejects_non_positive_size(plugin: Any) -> None:
    regions = [{"plugin_id": "fake_a", "x": 0, "y": 0, "w": 0, "h": 60, "settings": {}}]
    error = plugin.validate_settings({"regionsJson": json.dumps(regions)})
    assert error is not None
    assert "positive" in error


def test_validate_settings_rejects_negative_offset(plugin: Any) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": -1, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    error = plugin.validate_settings({"regionsJson": json.dumps(regions)})
    assert error is not None
    assert "negative" in error


def test_validate_settings_accepts_valid_regions(plugin: Any) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 800, "h": 60, "settings": {}}
    ]
    assert plugin.validate_settings({"regionsJson": json.dumps(regions)}) is None


def test_validate_settings_does_not_assume_an_800x480_canvas(plugin: Any) -> None:
    """A full-screen region on a larger panel must still be savable.

    `validate_settings` gets no `device_config`, so it cannot know the real
    resolution; bounds-checking against a hardcoded default here made the
    plugin unusable on anything bigger than 800x480.
    """
    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 1600, "h": 1200, "settings": {}}
    ]
    assert plugin.validate_settings({"regionsJson": json.dumps(regions)}) is None


def test_validate_settings_rejects_negative_refresh_minutes(plugin: Any) -> None:
    regions = [
        {
            "plugin_id": "fake_a",
            "x": 0,
            "y": 0,
            "w": 100,
            "h": 60,
            "settings": {},
            "refresh_minutes": -5,
        }
    ]
    error = plugin.validate_settings({"regionsJson": json.dumps(regions)})
    assert error is not None
    assert "refresh_minutes" in error


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
        plugin.generate_image(_as_subprocess_would(settings), device_config)
        plugin.generate_image(_as_subprocess_would(settings), device_config)

    assert FakeColorPlugin.call_counts["fake_a"] == 1
    # The cache must survive on disk, not in the settings dict — the dict is
    # a per-refresh copy in production and cannot carry state forward.
    assert "_layout_cache" not in settings
    assert list((tmp_path / "plugin_images" / "layout").glob("*.png"))


def test_expired_cache_re_invokes_child_plugin(plugin: Any, tmp_path: Path) -> None:
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
        plugin.generate_image(_as_subprocess_would(settings), device_config)
        # Backdate the cache file past its 15-minute window.
        for cached in (tmp_path / "plugin_images" / "layout").glob("*.png"):
            old = time.time() - 3600
            os.utime(cached, (old, old))
        plugin.generate_image(_as_subprocess_would(settings), device_config)

    assert FakeColorPlugin.call_counts["fake_a"] == 2


def test_region_without_refresh_minutes_always_refreshes(
    plugin: Any, tmp_path: Path
) -> None:
    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}

    with _patch_get_plugin_instance():
        plugin.generate_image(_as_subprocess_would(settings), device_config)
        plugin.generate_image(_as_subprocess_would(settings), device_config)

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
        first = plugin.generate_image(_as_subprocess_would(settings), device_config)
        FakeColorPlugin.fail_ids.add("fake_a")
        second = plugin.generate_image(_as_subprocess_would(settings), device_config)

    # Region content (excluding the border) should be identical: the stale
    # cached render was reused rather than a blank error placeholder.
    assert first.getpixel((50, 30)) == second.getpixel((50, 30)) == (255, 0, 0)


def test_stale_cache_files_are_pruned(plugin: Any, tmp_path: Path) -> None:
    from plugins.layout.layout import CACHE_MAX_AGE_DAYS

    regions = [
        {"plugin_id": "fake_a", "x": 0, "y": 0, "w": 100, "h": 60, "settings": {}}
    ]
    device_config = FakeDeviceConfig(tmp_path, _plugins_map("fake_a"))
    settings = {"regionsJson": json.dumps(regions)}
    cache_dir = tmp_path / "plugin_images" / "layout"
    cache_dir.mkdir(parents=True)

    # An orphan left behind by a region that was since moved or deleted.
    orphan = cache_dir / "9_gone_deadbeef00.png"
    Image.new("RGB", (10, 10), "white").save(orphan)
    expired = time.time() - (CACHE_MAX_AGE_DAYS + 1) * 86400
    os.utime(orphan, (expired, expired))

    with _patch_get_plugin_instance():
        plugin.generate_image(_as_subprocess_would(settings), device_config)

    assert not orphan.exists()
    # The region rendered this pass keeps its own fresh cache file.
    assert list(cache_dir.glob("*.png"))


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


def _write_fake_plugins_dir(root: Path) -> Path:
    """Build a plugins/ directory shaped like InkyPi's, for _list_available_plugins.

    Patched in rather than read from whatever InkyPi checkout happens to be
    present, so this test asserts the same thing in the standalone and
    inside-InkyPi environments.
    """
    plugins_dir = root / "plugins"
    for plugin_id, display_name in (
        ("weather", "Weather"),
        ("calendar", "Calendar"),
        ("layout", "Layout"),
    ):
        directory = plugins_dir / plugin_id
        directory.mkdir(parents=True)
        (directory / "plugin-info.json").write_text(
            json.dumps({"id": plugin_id, "display_name": display_name})
        )
    # base_plugin is excluded by name; a stray directory with no
    # plugin-info.json must be skipped rather than crash the scan.
    (plugins_dir / "base_plugin").mkdir()
    (plugins_dir / "not_a_plugin").mkdir()
    return plugins_dir


def test_generate_settings_template_lists_available_plugins_and_presets(
    plugin: Any, tmp_path: Path
) -> None:
    plugins_dir = _write_fake_plugins_dir(tmp_path)

    with patch("plugins.layout.layout.PLUGINS_DIR", str(plugins_dir)):
        template_params = plugin.generate_settings_template()

    ids = {p["id"] for p in template_params["available_plugins"]}
    assert ids == {"weather", "calendar"}
    # "layout" must not offer itself as a region target, and non-plugin
    # directories must not appear at all.
    assert "layout" not in ids
    assert "base_plugin" not in ids
    assert "not_a_plugin" not in ids
    # Sorted by display name for a stable dropdown order.
    assert [p["display_name"] for p in template_params["available_plugins"]] == [
        "Calendar",
        "Weather",
    ]

    assert "presets_json" in template_params
    presets = json.loads(template_params["presets_json"])
    assert "home" in presets
    assert len(presets["home"]) == 4


def test_presets_are_expressed_as_canvas_fractions(plugin: Any) -> None:
    """Presets must be resolution-independent.

    The settings UI multiplies these by the device's real canvas size, so a
    pixel-valued preset would only ever be correct on one panel — and would
    fail validation outright on a smaller one.
    """
    presets = json.loads(plugin.generate_settings_template()["presets_json"])
    for regions in presets.values():
        for region in regions:
            for key in ("x", "y", "w", "h"):
                assert (
                    0 <= region[key] <= 1
                ), f"{region['plugin_id']}.{key} not a fraction"
            assert region["x"] + region["w"] <= 1
            assert region["y"] + region["h"] <= 1
