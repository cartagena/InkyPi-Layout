import hashlib
import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from PIL import Image, ImageDraw

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.base_plugin.settings_schema import schema, section, widget
from plugins.plugin_registry import get_plugin_instance
from utils.app_utils import resolve_path

logger = logging.getLogger(__name__)

PLUGINS_DIR = resolve_path("plugins")

REQUIRED_REGION_KEYS = ("plugin_id", "x", "y", "w", "h")

# Orphaned region cache files (left behind when a region is moved, resized,
# reconfigured, or deleted) are swept once their mtime passes this age. Age
# rather than "not in the current region set" because several Layout playlist
# instances share one cache directory — set-based pruning would have each
# instance delete the others' still-live entries on every refresh.
CACHE_MAX_AGE_DAYS = 14

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

    def build_settings_schema(self) -> dict[str, object]:
        # The region editor (canvas, drag/resize, clone-picker, per-region
        # settings forms) is a bespoke UI that doesn't map onto individual
        # schema fields — it's wrapped whole as a single widget so this
        # plugin still participates in the schema-driven settings system
        # (consistent chrome, discoverability) without changing any of its
        # own markup/JS. settings.html itself is unchanged; it's now reached
        # via this widget's `template=` instead of the legacy fallback path.
        return schema(
            section(
                "Regions",
                widget("layout-regions", template="layout/settings.html"),
            ),
        )

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

        canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
        draw = ImageDraw.Draw(canvas)
        now = datetime.now(UTC)
        self._prune_cache_dir(device_config, now)

        for index, region in enumerate(regions):
            region_image = self._render_region(region, index, device_config, now)
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
    #
    # The cache is *entirely* on-disk: a PNG per region under
    # <plugin_image_dir>/layout/, with the file's own mtime as the "cached at"
    # timestamp. Nothing about it is kept in the `settings` dict. That's not a
    # style preference — InkyPi runs generate_image() in a subprocess
    # (INKYPI_PLUGIN_ISOLATION defaults to "process"), so `settings` arrives as
    # a pickled copy and anything written back into it is discarded when the
    # child exits. An earlier version stored the cache index in
    # settings["_layout_cache"], which meant every refresh started with an
    # empty index: `refresh_minutes` never actually skipped a child call, and
    # the stale-image fallback below never found an image to fall back to.

    def _render_region(
        self,
        region: dict[str, Any],
        index: int,
        device_config: Any,
        now: datetime,
    ) -> Image.Image:
        w, h = region["w"], region["h"]
        cache_path = self._region_cache_path(region, index, device_config)
        refresh_minutes = region.get("refresh_minutes")

        if refresh_minutes and cache_path:
            cached_at = self._cache_timestamp(cache_path)
            if cached_at and now - cached_at < timedelta(minutes=refresh_minutes):
                cached_image = self._load_cached_image(cache_path, w, h)
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
            fallback = self._load_cached_image(cache_path, w, h)
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

        self._store_cache(cache_path, image)
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

    def _region_cache_path(
        self, region: dict[str, Any], index: int, device_config: Any
    ) -> str | None:
        cache_dir = self._cache_dir(device_config)
        if cache_dir is None:
            return None
        return os.path.join(cache_dir, f"{self._region_cache_key(region, index)}.png")

    @staticmethod
    def _store_cache(cache_path: str | None, image: Image.Image) -> None:
        if cache_path is None:
            return
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            image.save(cache_path, format="PNG")
        except OSError as e:
            logger.warning("Layout: failed to write region cache file: %s", e)

    @staticmethod
    def _cache_timestamp(cache_path: str) -> datetime | None:
        """Return when *cache_path* was last written, as an aware UTC datetime."""
        try:
            return datetime.fromtimestamp(os.path.getmtime(cache_path), UTC)
        except OSError:
            return None

    @staticmethod
    def _load_cached_image(
        cache_path: str | None, width: int, height: int
    ) -> Image.Image | None:
        if not cache_path or not os.path.isfile(cache_path):
            return None
        try:
            with Image.open(cache_path) as cached:
                cached.load()
                if cached.size != (width, height):
                    return None
                return cached.convert("RGB")
        except Exception:
            return None

    def _prune_cache_dir(self, device_config: Any, now: datetime) -> None:
        """Delete region cache files older than ``CACHE_MAX_AGE_DAYS``.

        Every edit to a region's geometry or settings changes its cache key
        and so orphans the previous file; without this the cache directory
        grows for the life of the install. Pruning by age rather than by "not
        referenced by the regions I just rendered" is deliberate — multiple
        Layout playlist instances share this one directory, and a
        set-difference sweep would have each instance delete the others'
        still-live entries on every refresh.
        """
        cache_dir = self._cache_dir(device_config)
        if cache_dir is None or not os.path.isdir(cache_dir):
            return
        cutoff = now - timedelta(days=CACHE_MAX_AGE_DAYS)
        try:
            with os.scandir(cache_dir) as entries:
                for entry in entries:
                    if not entry.is_file() or not entry.name.endswith(".png"):
                        continue
                    cached_at = self._cache_timestamp(entry.path)
                    if cached_at is None or cached_at >= cutoff:
                        continue
                    try:
                        os.remove(entry.path)
                    except OSError as e:
                        logger.warning(
                            "Layout: failed to prune stale cache file %s: %s",
                            entry.path,
                            e,
                        )
        except OSError as e:
            logger.warning("Layout: failed to scan region cache directory: %s", e)

    @staticmethod
    def _render_error_placeholder(region: dict[str, Any], message: str) -> Image.Image:
        w, h = region["w"], region["h"]
        image = Image.new("RGB", (w, h), "#f0f0f0")
        draw = ImageDraw.Draw(image)
        # The reason goes on the panel as well as in the log: this box is
        # often the only symptom an operator sees, and "unavailable" alone
        # doesn't distinguish a missing API key from an upstream outage.
        draw.text(
            (6, 6),
            f"{region['plugin_id']}\nunavailable\n{message[:120]}",
            fill="black",
        )
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
                if refresh_minutes < 0:
                    raise RuntimeError(f"Region {i} has a negative refresh_minutes.")

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
