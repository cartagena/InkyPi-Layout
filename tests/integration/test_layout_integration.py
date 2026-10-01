"""Integration tests: Layout against a real InkyPi host.

What earns a place here is anything that can only be verified against real
host code — the plugin loading through InkyPi's own registry, the real
``BasePlugin`` contract, the real ``Config``, and a real installed child
plugin being composited. Everything expressible with fakes belongs in
``tests/unit/``, which runs everywhere and runs fast.

Child plugins are chosen for having no external API and no credentials
(``year_progress``, ``clock``) — a CI run must never depend on a third party
being up.
"""

import json
from typing import Any

import pytest
from PIL import Image


@pytest.fixture()
def layout_plugin() -> Any:
    """Load Layout the way InkyPi itself does, through the real registry."""
    from plugins.plugin_registry import get_plugin_instance, load_plugins

    plugin_config = {"id": "layout", "class": "Layout"}
    load_plugins([plugin_config])
    return get_plugin_instance(plugin_config)


def test_plugin_loads_through_the_real_registry(layout_plugin: Any) -> None:
    from plugins.base_plugin.base_plugin import BasePlugin

    assert isinstance(layout_plugin, BasePlugin)
    assert layout_plugin.get_plugin_id() == "layout"


def test_plugin_info_json_matches_the_registered_class(layout_plugin: Any) -> None:
    """The installed folder name, id, and class must agree.

    `inkypi plugin install` sparse-checkouts the folder named after the plugin
    id, so a mismatch here breaks installation for every end user while
    everything still works locally.
    """
    from utils.app_utils import resolve_path

    info_path = resolve_path("plugins/layout/plugin-info.json")
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)

    assert info["id"] == "layout"
    assert info["class"] == type(layout_plugin).__name__


def test_settings_schema_renders_through_the_real_template(layout_plugin: Any) -> None:
    """Render the schema through InkyPi's real settings_schema.html.

    Layout wraps its whole region editor as a single ``widget`` item; if the
    host's macros stop honouring ``template=``, the settings page silently
    renders an empty section instead of erroring.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from utils.app_utils import resolve_path

    env = Environment(
        loader=FileSystemLoader(
            [resolve_path("templates"), resolve_path("plugins")]
        ),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("settings_schema.html")

    params = layout_plugin.generate_settings_template()
    html = template.render(
        settings_schema=layout_plugin.build_settings_schema(),
        plugin_settings={},
        available_plugins=params["available_plugins"],
        presets_json=params["presets_json"],
        preset_labels=params["preset_labels"],
    )

    # The region editor's own markup made it through the widget indirection.
    assert 'name="regionsJson"' in html
    assert "layout-add-region" in html


def test_lists_real_installed_plugins(layout_plugin: Any) -> None:
    """_list_available_plugins scans the real plugins/ directory."""
    available = layout_plugin.generate_settings_template()["available_plugins"]
    ids = {p["id"] for p in available}

    assert "layout" not in ids, "Layout must not offer itself as a region target"
    # Built-ins that ship with every InkyPi checkout.
    assert {"clock", "weather"} <= ids
    assert all(p["display_name"] for p in available)


def test_composites_real_child_plugins_onto_one_canvas(
    layout_plugin: Any, device_config: Any
) -> None:
    """The whole point of the plugin, end to end, with real children.

    ``year_progress`` and ``clock`` are used because they render entirely from
    local state — no API key, no network, so this can't flake on a third party.
    Both go through the real Chromium screenshot path.
    """
    from plugins.plugin_registry import load_plugins

    load_plugins(
        [
            {"id": "layout", "class": "Layout"},
            {"id": "year_progress", "class": "YearProgress"},
            {"id": "clock", "class": "Clock"},
        ]
    )

    regions = [
        {"plugin_id": "year_progress", "x": 0, "y": 0, "w": 800, "h": 240, "settings": {}},
        {"plugin_id": "clock", "x": 0, "y": 240, "w": 800, "h": 240, "settings": {}},
    ]
    image = layout_plugin.generate_image(
        {"regionsJson": json.dumps(regions)}, device_config
    )

    assert isinstance(image, Image.Image)
    assert image.size == (800, 480)
    # Both regions rendered something rather than leaving white canvas.
    assert len(image.crop((0, 0, 800, 240)).getcolors(maxcolors=1 << 20)) > 1
    assert len(image.crop((0, 240, 800, 480)).getcolors(maxcolors=1 << 20)) > 1


def test_region_cache_survives_a_fresh_settings_dict(
    layout_plugin: Any, device_config: Any
) -> None:
    """The bug the unit suite can only model: caching across process boundaries.

    InkyPi pickles ``settings`` into a subprocess per refresh, so the cache
    cannot live in that dict. Here the real ``Config``'s real
    ``plugin_image_dir`` is what has to carry it.
    """
    from plugins.plugin_registry import load_plugins

    load_plugins(
        [
            {"id": "layout", "class": "Layout"},
            {"id": "year_progress", "class": "YearProgress"},
        ]
    )

    regions = [
        {
            "plugin_id": "year_progress",
            "x": 0, "y": 0, "w": 400, "h": 200,
            "settings": {},
            "refresh_minutes": 120,
        }
    ]
    regions_json = json.dumps(regions)

    # A brand-new dict each call, exactly as the refresh subprocess receives it.
    layout_plugin.generate_image({"regionsJson": regions_json}, device_config)
    cached = list((__import__("pathlib").Path(device_config.plugin_image_dir) / "layout").glob("*.png"))
    assert cached, "region render should have been cached to disk"

    first_mtime = cached[0].stat().st_mtime_ns
    layout_plugin.generate_image({"regionsJson": regions_json}, device_config)

    assert cached[0].stat().st_mtime_ns == first_mtime, (
        "second refresh re-rendered the region despite refresh_minutes=120; "
        "the cache is not surviving a fresh settings dict"
    )
