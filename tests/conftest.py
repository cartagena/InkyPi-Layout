"""Test setup shared by the unit and integration suites.

The unit tests are written to run in **two** environments:

* **Standalone** — a bare checkout of this repo, no InkyPi anywhere. The host
  modules `layout.py` imports don't exist, so this file registers minimal
  stand-ins for them in ``sys.modules`` before the plugin is imported.
* **Inside a real InkyPi checkout** — with `<inkypi>/src` on ``PYTHONPATH``.
  The real host modules import fine, so nothing is stubbed and the exact same
  tests run against the real ``BasePlugin``.

That dual mode is the point. Stubs that only ever run against themselves drift
away from the host silently; because these tests also run unstubbed in CI's
integration job, a change to InkyPi's ``BasePlugin`` contract shows up as a
failure rather than as a stub that quietly still passes.

The stubs deliberately cover *only* what `layout.py` imports. If you add a new
host import to the plugin, add its stub here — don't widen these into a
general-purpose fake InkyPi.
"""

import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def inkypi_src() -> Path | None:
    """``<inkypi>/src`` from ``$INKYPI_PATH``, if it points at a real checkout."""
    raw = os.environ.get("INKYPI_PATH")
    if not raw:
        return None
    src = Path(raw).expanduser().resolve() / "src"
    return src if src.is_dir() else None


def _add_inkypi_to_path() -> None:
    """Put a configured InkyPi checkout on sys.path before anything imports.

    This has to happen here, in the *root* conftest, rather than in
    ``tests/integration/conftest.py``: pytest loads the root one first, and by
    the time the integration conftest ran, this file would already have decided
    InkyPi was absent and stubbed ``plugins`` into ``sys.modules`` — after
    which the real package can never be imported.
    """
    src = inkypi_src()
    if src is not None and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def inkypi_available() -> bool:
    """True when a real InkyPi checkout is importable (its src/ is on sys.path)."""
    try:
        import plugins.base_plugin.base_plugin  # noqa: F401
    except ImportError:
        return False
    return True


# --- stand-ins for the host modules layout.py imports -----------------------


class _StubBasePlugin:
    """The subset of InkyPi's BasePlugin that Layout actually uses.

    Layout never calls ``render_image`` — it composites child plugin output
    with PIL directly — so the Chromium screenshot path is genuinely absent
    here rather than faked.
    """

    def __init__(self, config, **dependencies):
        self.config = dict(config)
        self.dependencies = dependencies

    def get_plugin_id(self) -> str:
        plugin_id = self.config.get("id")
        return plugin_id if isinstance(plugin_id, str) else ""

    def get_plugin_dir(self, path=None) -> str:
        base = str(REPO_ROOT / self.get_plugin_id())
        return f"{base}/{path}" if path else base

    @staticmethod
    def get_oriented_dimensions(device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return dimensions

    def validate_settings(self, settings):
        return None

    def build_settings_schema(self):
        return None

    def generate_settings_template(self) -> dict:
        # Mirrors the real BasePlugin's return shape closely enough for
        # Layout's override, which adds keys to whatever super() returns.
        template_params: dict = {"style_settings": True}
        settings_schema = self.build_settings_schema()
        if settings_schema:
            template_params["settings_schema"] = settings_schema
        else:
            template_params["settings_template"] = f"{self.get_plugin_id()}/settings.html"
        template_params["frame_styles"] = []
        return template_params


def _settings_schema_stub() -> types.ModuleType:
    """Reimplements InkyPi's schema DSL — pure dict builders, no host state."""
    module = types.ModuleType("plugins.base_plugin.settings_schema")

    def option(value, label, **kwargs):
        return {"value": value, "label": label, **kwargs}

    def field(name, field_type="text", label=None, **kwargs):
        return {
            "kind": "field",
            "type": field_type,
            "name": name,
            "label": label or name,
            **kwargs,
        }

    def row(*items, **kwargs):
        return {"kind": "row", "items": list(items), **kwargs}

    def widget(widget_type, **kwargs):
        return {"kind": "widget", "widget_type": widget_type, **kwargs}

    def section(title, *items, **kwargs):
        return {"title": title, "items": list(items), **kwargs}

    def schema(*sections, **kwargs):
        return {"version": 1, "sections": list(sections), **kwargs}

    for fn in (option, field, row, widget, section, schema):
        setattr(module, fn.__name__, fn)
    return module


def _install_host_stubs() -> None:
    # `plugins` gets this repo's root as its search path, so `plugins.layout`
    # resolves to the real ./layout/ package directory (a namespace package —
    # it has no __init__.py, which is also how InkyPi ships it).
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.__path__ = [str(REPO_ROOT)]

    base_plugin_pkg = types.ModuleType("plugins.base_plugin")
    base_plugin_pkg.__path__ = []
    base_plugin_module = types.ModuleType("plugins.base_plugin.base_plugin")
    base_plugin_module.BasePlugin = _StubBasePlugin

    registry = types.ModuleType("plugins.plugin_registry")

    def get_plugin_instance(plugin_config):
        # Every unit test patches this; reaching it unpatched is a test bug,
        # not a scenario worth faking.
        raise AssertionError(
            "get_plugin_instance must be patched in unit tests "
            "(see _patch_get_plugin_instance)"
        )

    registry.get_plugin_instance = get_plugin_instance

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    app_utils = types.ModuleType("utils.app_utils")
    # Points at a directory that doesn't exist, so _list_available_plugins
    # takes its OSError path and returns []. Tests that care about the plugin
    # listing patch layout.PLUGINS_DIR at a fixture directory instead, which
    # keeps them deterministic in both environments.
    app_utils.resolve_path = lambda file_path: str(REPO_ROOT / "_no_such_dir" / file_path)

    sys.modules.update(
        {
            "plugins": plugins_pkg,
            "plugins.base_plugin": base_plugin_pkg,
            "plugins.base_plugin.base_plugin": base_plugin_module,
            "plugins.base_plugin.settings_schema": _settings_schema_stub(),
            "plugins.plugin_registry": registry,
            "utils": utils_pkg,
            "utils.app_utils": app_utils,
        }
    )


_add_inkypi_to_path()

if inkypi_available():
    pass
elif inkypi_src() is not None:
    # INKYPI_PATH points at a real checkout but the host still won't import —
    # almost always a missing InkyPi dependency. Fail loudly: silently falling
    # back to stubs here would let CI's integration job report green while
    # having quietly run the unit suite twice.
    import plugins.base_plugin.base_plugin as _probe  # noqa: F401
else:
    _install_host_stubs()
