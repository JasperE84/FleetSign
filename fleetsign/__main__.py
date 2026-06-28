from __future__ import annotations

import argparse
import logging
import os
import signal
import tempfile
from pathlib import Path

from .config import AppConfig
from .logsetup import configure_logging
from .player import PlayerController
from .store import PlaylistStore
from .sync import SyncClient
from .web import MAX_UPLOAD_BYTES, create_app, create_slave_app

# Waitress caps the request body at 1 GiB and drops idle channels after 120 s by
# default — both too small for large video uploads over slow WiFi.
UPLOAD_CHANNEL_TIMEOUT = 600  # seconds


def build(root: Path, host: str, port: int):
    config = AppConfig.load_or_create(root, host=host, port=port)
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    socket_path = str(config.data_dir / "mpv.sock")
    controller = PlayerController(store, socket_path, web_port=config.port)
    if config.is_slave():
        sync = SyncClient(store, config)
        app = create_slave_app(store, config, controller, sync)
    else:
        sync = None
        app = create_app(store, config, controller)
    return app, controller, config, sync


def serve_kwargs(config: AppConfig) -> dict:
    return {
        "host": config.host,
        "port": config.port,
        "max_request_body_size": MAX_UPLOAD_BYTES,
        "channel_timeout": UPLOAD_CHANNEL_TIMEOUT,
    }


def main() -> None:
    # Configure logging first thing (main() only, like tempfile.tempdir below) so
    # everything from config load onward lands in journald at the right level.
    configure_logging()
    log = logging.getLogger("fleetsign")

    parser = argparse.ArgumentParser(description="FleetSign daemon")
    parser.add_argument("--root", default=os.environ.get("FLEETSIGN_ROOT", "."))
    parser.add_argument("--host", default=os.environ.get("FLEETSIGN_HOST", "0.0.0.0"))
    port_env = os.environ.get("FLEETSIGN_PORT", "8080")
    try:
        port_default = int(port_env)
    except ValueError:
        parser.error(f"FLEETSIGN_PORT={port_env!r} is not a valid integer")
    parser.add_argument("--port", type=int, default=port_default)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    app, controller, config, sync = build(root, args.host, args.port)
    role = "slave" if config.is_slave() else "master"
    log.info("FleetSign starting: role=%s root=%s serving http://%s:%d",
             role, root, config.host, config.port)
    # Route large multipart upload temp files onto the data filesystem (not a
    # tmpfs /tmp), so a 250 MB+ upload can't exhaust RAM and the final save is a
    # same-filesystem move. Done only in main(), never in build(), so tests that
    # call build() don't mutate the global temp dir.
    tempfile.tempdir = str(config.data_dir)

    # Register the shutdown handler before starting the controller so a SIGTERM
    # during startup still stops mpv cleanly. shutdown() joins the player thread
    # and tears down mpv synchronously before exit. (A single log line from a
    # signal handler has a tiny reentrancy risk, but this daemon's log volume
    # makes interrupting an in-progress emit effectively impossible.)
    def _on_sigterm(*_):
        log.info("SIGTERM received; shutting down")
        controller.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)
    controller.start()
    if sync is not None:
        sync.start()

    from waitress import serve
    serve(app, **serve_kwargs(config))


if __name__ == "__main__":
    main()
