Module 1 - Personal Website (Flask)
JHU EN.605.256 Modern Software Concepts in Python
Author: Mike Sasso

WHAT THIS IS
------------
A personal developer website built with Flask, HTML, and CSS. It has three
pages -- Home, Contact, and Projects -- served through an application factory
and a Flask blueprint, with a shared navigation bar (top-right) that highlights
the current tab.

PROJECT LAYOUT
--------------
module_1/
  run.py               Entry point. Starts the server on port 8080.
  requirements.txt     Exact package versions to rebuild the environment.
  README.txt           This file.
  app/
    __init__.py        create_app() application factory.
    pages/
      __init__.py
      routes.py        Home / Contact / Projects routes + editable content.
    templates/         Jinja2 HTML templates (base + one per page).
    static/
      css/style.css    Site styles (nav color, layout, cards).
      img/profile.svg  Placeholder photo -- replace with your own (see below).

REQUIREMENTS
------------
- Python 3.10 or newer (developed and tested on 3.14).

SETUP AND RUN
-------------
1. Open a terminal in this module_1/ folder.
2. Create and activate a virtual environment:
     python3 -m venv .venv
     source .venv/bin/activate         (Windows: .venv\Scripts\activate)
3. Install dependencies:
     pip install -r requirements.txt
4. Start the site:
     python run.py
5. Visit:
     http://localhost:8080
Press Ctrl+C in the terminal to stop the server.

PERSONALIZE
-----------
- Edit PROFILE and PROJECTS at the top of app/pages/routes.py
  (name, position, bio, email, LinkedIn URL, project details).
- Replace app/static/img/profile.svg with your own photo. If you use a JPG,
  save it as app/static/img/profile.jpg and change the "photo" value in
  routes.py to "img/profile.jpg".
