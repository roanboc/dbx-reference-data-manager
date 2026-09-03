"""Entrypoint for the Reference Data Manager (Dash).

* ``python app.py --dev``  - Dash development server with hot reload (local work)
* ``python app.py``        - gunicorn (what ``app.yaml`` runs on Databricks Apps)

The WSGI application is exposed as ``server`` for ``gunicorn app:server`` as well.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rdm.ui.app import create_app  # noqa: E402
from rdm.ui.context import get_settings  # noqa: E402

app = create_app()
server = app.server


def _port() -> int:
    return int(os.environ.get("DATABRICKS_APP_PORT") or os.environ.get("PORT") or 8050)


def serve() -> None:
    """Run under gunicorn. DuckDB allows one writer process, so it gets a single worker."""
    from gunicorn.app.base import BaseApplication

    settings = get_settings()
    workers = 1 if settings.backend == "duckdb" else int(os.environ.get("RDM_WORKERS", "2"))

    class Server(BaseApplication):
        def __init__(self, application, options):
            self.options = options
            self.application = application
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                self.cfg.set(key, value)

        def load(self):
            return self.application

    Server(
        server,
        {
            "bind": f"0.0.0.0:{_port()}",
            "workers": workers,
            "threads": 4,
            "timeout": 120,
            "accesslog": "-",
            "loglevel": "info",
        },
    ).run()


if __name__ == "__main__":
    if "--dev" in sys.argv:
        app.run(host="0.0.0.0", port=_port(), debug=True)
    else:
        serve()
