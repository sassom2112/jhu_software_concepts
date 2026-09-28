"""
run.py - Start the Grad Café analysis web page.

Usage::

    python run.py            # serves the page on port 8080 of this machine

FLASK_HOST / PORT change the address.  Debug mode stays off; set
FLASK_DEBUG=1 only on a development machine.
"""

import os

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
