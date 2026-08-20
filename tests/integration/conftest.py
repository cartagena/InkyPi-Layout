"""Integration-suite setup: requires a real InkyPi checkout.

Point ``INKYPI_PATH`` at a jtn0123/InkyPi checkout and this suite runs against
the real host — the real ``BasePlugin``, the real plugin registry, the real
``Config``. Without it every test here is skipped, so a bare ``pytest`` from a
clean clone still exits green having run the unit suite.

The plugin folder must also be reachable at ``<inkypi>/src/plugins/layout``
(symlink or copy) — that's how InkyPi's own loader finds a plugin, and
``BasePlugin`` resolves ``render_dir``/``get_plugin_dir`` relative to it.
"""

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_ID = "layout"


def _inkypi_src() -> Path | None:
    """Read $INKYPI_PATH directly rather than importing tests/conftest.py.

    `tests/` is not a package, so importing across conftest files would rely on
    pytest's rootdir being on sys.path — true today, but a silent breakage if
    anyone runs this suite from a different working directory.
    """
    raw = os.environ.get("INKYPI_PATH")
    if not raw:
        return None
    src = Path(raw).expanduser().resolve() / "src"
    return src if src.is_dir() else None


# Resolved at import time so the skip reason names the actual problem, rather
# than surfacing as an identical ImportError inside every test. sys.path setup
# already happened in the root conftest — see _add_inkypi_to_path there.
INKYPI_SRC = _inkypi_src()

if INKYPI_SRC is None:
    _SKIP_REASON = (
        "Set INKYPI_PATH to a jtn0123/InkyPi checkout to run the integration suite "
        "(see the Testing section of README.md)."
    )
elif not (INKYPI_SRC / "plugins" / PLUGIN_ID).exists():
    _SKIP_REASON = (
        f"'{PLUGIN_ID}' is not installed into $INKYPI_PATH/src/plugins/. "
        f"Symlink it: ln -s {REPO_ROOT / PLUGIN_ID} {INKYPI_SRC / 'plugins' / PLUGIN_ID}"
    )
else:
    _SKIP_REASON = ""

# collect_ignore_glob skips at *collection* time, so an unconfigured run never
# even imports these modules (which would fail on their host-only imports).
collect_ignore_glob = ["test_*.py"] if _SKIP_REASON else []


@pytest.fixture()
def device_config(tmp_path, monkeypatch):
    """A real InkyPi ``Config`` backed by a throwaway device.json.

    Deliberately built here rather than reused from InkyPi's own
    ``tests/conftest.py``: that fixture isn't importable unless the test file
    physically lives inside the InkyPi checkout, which is exactly the copy-file
    workflow this suite exists to replace.
    """
    config_file = tmp_path / "device.json"
    config_file.write_text(
        json.dumps(
            {
                "name": "InkyPi Layout Integration",
                "display_type": "mock",
                "resolution": [800, 480],
                "orientation": "horizontal",
                "timezone": "UTC",
                "output_dir": str(tmp_path / "mock_output"),
                "plugin_cycle_interval_seconds": 300,
                "image_settings": {},
                "playlist_config": {"playlists": [], "active_playlist": ""},
                "refresh_info": {
                    "refresh_time": None,
                    "image_hash": None,
                    "refresh_type": "Manual Update",
                    "plugin_id": "",
                },
            }
        )
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))

    import config as config_mod

    monkeypatch.setattr(config_mod.Config, "config_file", str(config_file))
    monkeypatch.setattr(
        config_mod.Config, "current_image_file", str(tmp_path / "current.png")
    )
    monkeypatch.setattr(
        config_mod.Config, "processed_image_file", str(tmp_path / "processed.png")
    )
    monkeypatch.setattr(config_mod.Config, "plugin_image_dir", str(tmp_path / "plugins"))
    monkeypatch.setattr(config_mod.Config, "history_image_dir", str(tmp_path / "history"))
    (tmp_path / "plugins").mkdir(exist_ok=True)
    (tmp_path / "history").mkdir(exist_ok=True)

    return config_mod.Config()
