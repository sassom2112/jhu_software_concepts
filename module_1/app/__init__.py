"""Application factory and blueprint registration.

Keeping the routes in their own blueprint (app/pages) means new course
modules can add their own blueprints later without touching this file much.
"""
from flask import Flask


def create_app():
    """Build, configure, and return the Flask application instance."""
    # Flask(__name__) makes ./templates and ./static (next to this package)
    # the default template and static folders.
    app = Flask(__name__)

    # Register the "pages" blueprint that serves all human-facing routes.
    from app.pages.routes import pages_bp
    app.register_blueprint(pages_bp)

    return app
