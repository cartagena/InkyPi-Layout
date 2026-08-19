import hashlib
import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from PIL import Image, ImageDraw

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.plugin_registry import get_plugin_instance
from utils.app_utils import resolve_path

logger = logging.getLogger(__name__)

PLUGINS_DIR = resolve_path("plugins")

REQUIRED_REGION_KEYS = ("plugin_id", "x", "y", "w", "h")

# Concrete "Home screen" preset used as this plugin's real-world test case.
HOME_PRESET = [
    {"plugin_id": "weather", "x": 0, "y": 0, "w": 800, "h": 60, "settings": {}},
    {
        "plugin_id": "blood_sugar",
        "x": 0,
        "y": 60,
        "w": 220,
        "h": 186,
        "settings": {},
        "refresh_minutes": 15,
    },
    {
        "plugin_id": "calendar",
        "x": 220,
        "y": 60,
        "w": 580,
        "h": 186,
        "settings": {},
    },
    {
        "plugin_id": "nutrislice",
        "x": 0,
        "y": 246,
        "w": 800,
        "h": 60,
        "settings": {"daysToShow": "1", "showCarbs": "true"},
        "refresh_minutes": 240,
    },
]

PRESETS: dict[str, dict[str, Any]] = {
    "home": {"label": "Home screen", "regions": HOME_PRESET},
}


class _RegionDeviceConfig:
    """DeviceConfigLike proxy that reports a region's (w, h) as the resolution.

    Wraps the real device_config so a child plugin's own internal layout logic
    (font sizes, wrapping, spacing) adapts to the region's actual pixel size
    instead of rendering full-screen and getting cropped down. Orientation is
    pinned to "horizontal" so `BasePlugin.get_oriented_dimensions()` — which
    some plugins call instead of `get_resolution()` directly — doesn't swap
    width/height based on the *device's* orientation setting; the region's
    w/h is already the exact size the child should render at. Everything
    else (timezone, API keys via load_env_key, plugin_image_dir, etc.)
    delegates straight through to the real device_config.
    """

    def __init__(self, device_config: Any, width: int, height: int) -> None:
        self._device_config = device_config
        self._width = width
        self._height = height

    def get_resolution(self) -> tuple[int, int]:
        return (self._width, self._height)

    def get_config(self, key: str | None = None, default: Any = None) -> Any:
        if key == "orientation":
            return "horizontal"
        return self._device_config.get_config(key, default)

    def load_env_key(self, key: str) -> str | None:
        return cast("str | None", self._device_config.load_env_key(key))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._device_config, name)


class Layout(BasePlugin):
    """Composes multiple child plugins into named regions on one canvas.

    Each region is rendered by calling the target plugin's own
    `generate_image()` as a black box, scoped to the region's pixel size via
    `_RegionDeviceConfig`, then pasted onto the full canvas at (x, y).
    """

    def validate_settings(self, settings: Mapping[str, object]) -> str | None:
        try:
            regions = self._parse_regions(settings)
        except RuntimeError as e:
            return str(e)
        # validate_settings has no device_config, so bounds are checked
        # against this plugin's documented target canvas (800x480).
        # generate_image() re-validates against the real configured
        # resolution, which is authoritative.
        for region in regions:
            error = self._validate_region(region, canvas_width=800, canvas_height=480)
            if error:
                return error
        return None

    def generate_settings_template(self) -> dict[str, object]:
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["available_plugins"] = self._list_available_plugins()
        template_params["presets_json"] = json.dumps(
            {key: preset["regions"] for key, preset in PRESETS.items()}
        )
        template_params["preset_labels"] = [
            {"key": key, "label": preset["label"]} for key, preset in PRESETS.items()
        ]
        return template_params

    def generate_image(self, settings: Any, device_config: Any) -> Image.Image:
        regions = self._parse_regions(settings)
        if not regions:
            raise RuntimeError("At least one region must be configured.")

        canvas_w, canvas_h = self.get_oriented_dimensions(device_config)

        for region in regions:
            error = self._validate_region(region, canvas_w, canvas_h)
            if error:
                raise RuntimeError(error)
            # A missing/unregistered plugin_id is a configuration mistake,
            # not a transient upstream failure — fail loudly here rather
            # than letting _render_region's graceful-degradation path (for
            # genuine runtime API failures) hide a typo behind a vague
            # placeholder.
            if not device_config.get_plugin(region["plugin_id"]):
                raise RuntimeError(
                    f"Plugin '{region['plugin_id']}' is not installed/registered."
                )

        cache = settings.get("_layout_cache")
        if not isinstance(cache, dict):
            cache = {}
            settings["_layout_cache"] = cache

        canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
        draw = ImageDraw.Draw(canvas)
        now = datetime.now(UTC)

        for index, region in enumerate(regions):
            region_image = self._render_region(region, index, device_config, cache, now)
            canvas.paste(region_image, (region["x"], region["y"]))
            draw.rectangle(
                [
                    region["x"],
                    region["y"],
                    region["x"] + region["w"] - 1,
                    region["y"] + region["h"] - 1,
                ],
                outline="black",
                width=1,
            )

        return canvas

    # ---- region rendering / per-region caching ----
    #
    # This cache only protects *child plugins* from being invoked (and thus
    # hitting their upstream APIs) more often than `refresh_minutes` calls
    # for. It does NOT reduce how often the physical e-paper panel itself
    # refreshes/flashes — that cadence is controlled entirely by this Layout
    # plugin instance's own playlist refresh interval, which is the
    # operator's responsibility to set appropriately (e.g. to the fastest
    # region's needs). generate_image() always recomposites and returns a
    # full canvas on every call; only the expensive child `generate_image()`
    # call — usually an external API request — is what gets skipped when a
    # region's cache is still fresh.

    def _render_region(
        self,
        region: dict[str, Any],
        index: int,
        device_config: Any,
        cache: dict[str, Any],
        now: datetime,
    ) -> Image.Image:
        w, h = region["w"], region["h"]
        cache_key = self._region_cache_key(region, index)
        refresh_minutes = region.get("refresh_minutes")
        cached_entry = cache.get(cache_key)

        if refresh_minutes and cached_entry:
            cached_at = self._parse_iso(cached_entry.get("cached_at"))
            if cached_at and now - cached_at < timedelta(minutes=refresh_minutes):
                cached_image = self._load_cached_image(cached_entry, w, h)
                if cached_image is not None:
                    return cached_image

        try:
            image = self._call_child_plugin(region, device_config)
        except Exception as e:
            logger.error(
                "Layout region %d ('%s') failed to render: %s",
                index,
                region["plugin_id"],
                e,
            )
            # Prefer a stale cached image over a blank error box — the rest
            # of the composite is still useful even if one region's data
            # source is temporarily unavailable.
            fallback = (
                self._load_cached_image(cached_entry, w, h) if cached_entry else None
            )
            if fallback is not None:
                logger.warning(
                    "Layout region %d ('%s') reusing stale cached image after "
                    "render failure.",
                    index,
                    region["plugin_id"],
                )
                return fallback
            return self._render_error_placeholder(region, str(e))

        if image.size != (w, h):
            logger.warning(
                "Plugin '%s' returned image %s, expected %s; resizing.",
                region["plugin_id"],
                image.size,
                (w, h),
            )
            image = image.resize((w, h))

        self._store_cache(cache, cache_key, image, now, device_config)
        return image

    def _call_child_plugin(
        self, region: dict[str, Any], device_config: Any
    ) -> Image.Image:
        plugin_id = region["plugin_id"]
        plugin_config = device_config.get_plugin(plugin_id)
        if not plugin_config:
            raise RuntimeError(f"Plugin '{plugin_id}' is not installed/registered.")

        child = get_plugin_instance(plugin_config)
        scoped_device_config = _RegionDeviceConfig(
            device_config, region["w"], region["h"]
        )
        region_settings = region.get("settings") or {}
        image = child.generate_image(region_settings, scoped_device_config)
        return image.convert("RGB")

    def _region_cache_key(self, region: dict[str, Any], index: int) -> str:
        payload = json.dumps(
            {
                "plugin_id": region["plugin_id"],
                "x": region["x"],
                "y": region["y"],
                "w": region["w"],
                "h": region["h"],
                "settings": region.get("settings") or {},
            },
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha1(
            payload.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:10]
        return f"{index}_{region['plugin_id']}_{digest}"

    def _cache_dir(self, device_config: Any) -> str | None:
        base = getattr(device_config, "plugin_image_dir", None)
        if not base:
            return None
        return os.path.join(base, "layout")

    def _store_cache(
        self,
        cache: dict[str, Any],
        cache_key: str,
        image: Image.Image,
        now: datetime,
        device_config: Any,
    ) -> None:
        cache_dir = self._cache_dir(device_config)
        if cache_dir is None:
            return
        try:
            os.makedirs(cache_dir, exist_ok=True)
            path = os.path.join(cache_dir, f"{cache_key}.png")
            image.save(path, format="PNG")
        except OSError as e:
            logger.warning("Layout: failed to write region cache file: %s", e)
            return
        cache[cache_key] = {"cached_at": now.isoformat(), "path": path}

    @staticmethod
    def _load_cached_image(
        cached_entry: dict[str, Any] | None, width: int, height: int
    ) -> Image.Image | None:
        path = cached_entry.get("path") if cached_entry else None
        if not path or not os.path.isfile(path):
            return None
        try:
            with Image.open(path) as cached:
                cached.load()
                if cached.size != (width, height):
                    return None
                return cached.convert("RGB")
        except Exception:
            return None

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _render_error_placeholder(region: dict[str, Any], message: str) -> Image.Image:
        w, h = region["w"], region["h"]
        image = Image.new("RGB", (w, h), "#f0f0f0")
        draw = ImageDraw.Draw(image)
        draw.text((6, 6), f"{region['plugin_id']}\nunavailable", fill="black")
        return image

    # ---- region config parsing / validation ----

    def _parse_regions(self, settings: Mapping[str, object]) -> list[dict[str, Any]]:
        raw = settings.get("regionsJson")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return []

        if isinstance(raw, str):
            try:
                regions = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Regions JSON is not valid JSON: {e}") from e
        else:
            regions = raw

        if not isinstance(regions, list):
            raise RuntimeError("Regions JSON must be a JSON array.")

        parsed: list[dict[str, Any]] = []
        for i, region in enumerate(regions):
            if not isinstance(region, dict):
                raise RuntimeError(f"Region {i} must be a JSON object.")

            missing = [k for k in REQUIRED_REGION_KEYS if k not in region]
            if missing:
                raise RuntimeError(
                    f"Region {i} is missing required field(s): {', '.join(missing)}."
                )

            plugin_id = region["plugin_id"]
            if not isinstance(plugin_id, str) or not plugin_id:
                raise RuntimeError(f"Region {i} has an invalid plugin_id.")
            if plugin_id == self.get_plugin_id():
                raise RuntimeError(
                    f"Region {i} targets the layout plugin itself, which isn't allowed."
                )

            try:
                x, y, w, h = (
                    int(region["x"]),
                    int(region["y"]),
                    int(region["w"]),
                    int(region["h"]),
                )
            except (TypeError, ValueError) as e:
                raise RuntimeError(f"Region {i} has non-integer x/y/w/h.") from e

            refresh_minutes = region.get("refresh_minutes")
            if refresh_minutes is not None:
                try:
                    refresh_minutes = int(refresh_minutes)
                except (TypeError, ValueError) as e:
                    raise RuntimeError(
                        f"Region {i} has a non-integer refresh_minutes."
                    ) from e

            region_settings = region.get("settings")
            if region_settings is None:
                region_settings = {}
            if not isinstance(region_settings, dict):
                raise RuntimeError(f"Region {i}'s settings must be a JSON object.")

            parsed.append(
                {
                    "plugin_id": plugin_id,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "settings": region_settings,
                    "refresh_minutes": refresh_minutes,
                }
            )

        return parsed

    @staticmethod
    def _validate_region(
        region: dict[str, Any], canvas_width: int, canvas_height: int
    ) -> str | None:
        x, y, w, h = region["x"], region["y"], region["w"], region["h"]
        plugin_id = region["plugin_id"]
        if w <= 0 or h <= 0:
            return f"Region for '{plugin_id}' must have positive width and height."
        if x < 0 or y < 0:
            return f"Region for '{plugin_id}' has a negative x or y."
        if x + w > canvas_width or y + h > canvas_height:
            return (
                f"Region for '{plugin_id}' at x={x}, y={y}, w={w}, h={h} exceeds the "
                f"{canvas_width}x{canvas_height} canvas."
            )
        return None

    def _list_available_plugins(self) -> list[dict[str, str]]:
        plugins: list[dict[str, str]] = []
        try:
            with os.scandir(PLUGINS_DIR) as entries:
                for entry in entries:
                    if not entry.is_dir() or entry.name in (
                        "base_plugin",
                        self.get_plugin_id(),
                    ):
                        continue
                    info_path = os.path.join(entry.path, "plugin-info.json")
                    if not os.path.isfile(info_path):
                        continue
                    try:
                        with open(info_path, encoding="utf-8") as f:
                            info = json.load(f)
                    except (OSError, json.JSONDecodeError):
                        continue
                    plugin_id = info.get("id") or entry.name
                    display_name = info.get("display_name") or plugin_id
                    plugins.append({"id": plugin_id, "display_name": display_name})
        except OSError as e:
            logger.warning("Layout: failed to scan plugins directory: %s", e)
        plugins.sort(key=lambda p: p["display_name"].lower())
        return plugins
