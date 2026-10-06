"""
run.py - Start the Grad Café analysis web page.

Usage::

    python -m web.run        # serves the page on port 8080 of this machine

The web package must be importable: run ``pip install -e .`` first (or put
src/ on PYTHONPATH, as the web image does).

FLASK_HOST and PORT choose the address.  The default host, 127.0.0.1, keeps a
development server private to this machine; the web container sets
FLASK_HOST=0.0.0.0 so Docker can forward port 8080 to it.  RABBITMQ_URL says
where the buttons queue their tasks, and DATABASE_URL (or DB_*) which read-only
role the page logs in as.  Debug mode stays off; set FLASK_DEBUG=1 only on a
development machine.
"""

import os

from web.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        # 127.0.0.1 outside Docker; the web image sets FLASK_HOST=0.0.0.0 (see web/Dockerfile).
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
