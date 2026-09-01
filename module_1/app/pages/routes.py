"""Route definitions for the personal website.

Every view renders a template and passes an ``active_page`` flag so the shared
navigation bar (defined in base.html) can highlight the current tab.

To personalize the site, edit the PROFILE and PROJECTS values below -- the
templates read from them, so you should not need to touch the HTML.
"""
from flask import Blueprint, render_template

# All page routes live under a single blueprint.
pages_bp = Blueprint("pages", __name__)

# --- Editable site content --------------------------------------------------
PROFILE = {
    "name": "Mike Sasso",
    "position": "Security Researcher & Developer",
    "bio": (
        "I am a first-semester graduate student at Johns Hopkins University "
        "studying software and security. I build defensive tooling, automate "
        "incident response, and I am currently learning modern software "
        "concepts in Python. This site will grow with every project I "
        "complete throughout the course."
    ),
    "photo": "img/profile.png",
    "email": "msasso1@jh.edu",
    "linkedin_url": "https://www.linkedin.com/in/michael-sasso-0x00/",
    "linkedin_label": "https://www.linkedin.com/in/michael-sasso-0x00/",
}

# Projects shown on the Projects page. Add one dict per project.
PROJECTS = [
    {
        "title": "Module 1 - Personal Website (Flask)",
        "description": (
            "A personal developer website built with Flask, HTML, and CSS. "
            "It uses an application factory and a blueprint to serve a home "
            "page, a contact page, and this projects page, sharing one "
            "navigation bar that highlights the active tab."
        ),
        "github_url": (
            "https://github.com/sassom2112/jhu_software_concepts/"
            "tree/master/module_1"
        ),
    },
]


@pages_bp.route("/")
def home():
    """Homepage: name, position, and bio on the left; photo on the right."""
    return render_template("home.html", active_page="home", profile=PROFILE)


@pages_bp.route("/contact")
def contact():
    """Contact page: email address and LinkedIn."""
    return render_template("contact.html", active_page="contact", profile=PROFILE)


@pages_bp.route("/projects")
def projects():
    """Projects page: project title, details, and a link to its GitHub."""
    return render_template(
        "projects.html", active_page="projects", projects=PROJECTS, profile=PROFILE
    )
