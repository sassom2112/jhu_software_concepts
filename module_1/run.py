"""Entry point for the Module 1 personal website.

Start the site with:
    python run.py
Then open http://localhost:8080 in a browser.
"""
from app import create_app

# create_app() is an application factory: it builds the Flask app on demand
# instead of at import time, which keeps configuration flexible and testable.
app = create_app()

if __name__ == "__main__":
    # SHALL: launch with `python run.py`.
    # SHALL: serve on port 8080, bound to 0.0.0.0 (also reachable as localhost).
    app.run(host="0.0.0.0", port=8080, debug=True)
