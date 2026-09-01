Module 1 - Personal Website (Flask)
JHU EN.605.256 Modern Software Concepts in Python
Author: Mike Sasso

WHAT THIS IS
------------
A personal developer website built with Flask, HTML, and CSS. It has three
pages: Home, Contact, and Projects all served through an application factory
and a Flask blueprint, with a shared navigation bar (top-right) that highlights
the current tab.

PROJECT LAYOUT
--------------
```
module_1/
├── README.txt
├── app
│   ├── __init__.py                          # create_app() application factory.
│   ├── __pycache__
│   │   └── __init__.cpython-314.pyc
│   ├── pages
│   │   ├── __init__.py
│   │   ├── __pycache__
│   │   │   ├── __init__.cpython-314.pyc
│   │   │   └── routes.cpython-314.pyc
│   │   └── routes.py                        # Home / Contact / Projects routes + editable content.
│   ├── static
│   │   ├── css
│   │   │   └── style.css                    # Site styles (nav color, layout, cards).
│   │   └── img
│   │       └── profile.svg
│   └── templates                            # Jinja2 HTML templates (base + one per page).
│       ├── base.html
│       ├── contact.html
│       ├── home.html
│       └── projects.html
├── requirements.txt                         # Exact package versions to rebuild the environment.
└── run.py                                   # Entry point. Starts the server on port 8080.
```


REQUIREMENTS
------------
- Python 3.10 or newer (developed and tested on 3.14).

SETUP AND RUN
-------------
1. Open a terminal in this module_1/ folder.
2. Create and activate a virtual environment:
     * python3 -m venv .venv
     * source .venv/bin/activate             # (Windows: .venv\Scripts\activate)
3. Install dependencies:
     * pip install -r requirements.txt
4. Start the site:
     * python run.py
5. Visit:
     * http://localhost:8080

Press Ctrl+C in the terminal to stop the server.
