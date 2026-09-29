"""Sphinx configuration for the Grad Café Analytics documentation (module_4/docs).

Build locally, from the module_4 folder:

    pip install -r docs/requirements.txt
    sphinx-build -W -b html docs/source docs/build/html

Read the Docs uses the same file through .readthedocs.yaml at the repository root.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The application code is not an installed package: make module_4/src importable
# so autodoc can read the docstrings of scrape.py, clean.py, load_data.py, ...
SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

project = "Grad Café Analytics"
author = "Mike Sasso"
copyright = "2026, Mike Sasso"
release = "Module 4"

extensions = [
    "sphinx.ext.autodoc",    # API reference from the docstrings
    "sphinx.ext.napoleon",   # "Args:" sections in Google-style docstrings
    "sphinx.ext.viewcode",   # "[source]" links next to every documented object
]

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "show-inheritance": True}

templates_path: list[str] = []
exclude_patterns: list[str] = []

# Literal blocks in the docstrings are shell commands and tables, not Python:
# show them as plain text (explicit ``code-block:: bash`` blocks still highlight).
highlight_language = "none"

html_theme = "sphinx_rtd_theme"
html_title = "Grad Café Analytics"
html_show_sourcelink = False
html_static_path = ["_static"]
html_css_files = ["custom.css"]   # wrap long table cells
