"""
setup.py - Makes module_6 an installable Python project.

    pip install -e .        (or:  uv pip install -e .)

installs the project in editable mode: the virtual environment points at src/
instead of holding a copy of it.  src/ holds three packages - web (the Flask
app, web.app), worker (the ETL scripts, worker.etl) and db (settings, loader,
roles; shared by both) - and every module then imports from any folder by its
full name (db.load_data, web.app, worker.etl.scrape, ...).  The gradcafe-load
and gradcafe-app-role commands below run from anywhere, without editing
sys.path.  A change to the code takes effect without reinstalling.
tests/conftest.py and docs/source/conf.py still put src/ on sys.path
themselves, so the test suite and the Sphinx build also work where the project
is not installed.

Keep the -e.  db.load_data reads its input from src/data/ (or from the folder
named by the DATA_DIR environment variable), and worker.etl's scrape, clean
and build_query_results read and write their files in module_6/, the parent of
src/.  A plain `pip install .` copies the modules into the virtual
environment, where they would look for those files in vain.

requirements.txt pins the exact versions that were tested.  install_requires
below only names what the application needs at run time, with the oldest
versions it works with, so the package can also be installed next to newer
releases.  The "dev" extra adds the test and code-quality tools.
"""

from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).resolve().parent

setup(
    name="gradcafe-analysis",
    version="0.5.0",
    description="Grad Cafe admissions scraper, PostgreSQL loader and Flask analysis page "
                "(JHU EN.605.256 Modern Software Concepts in Python, Module 5)",
    long_description=(HERE / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Mike Sasso",
    python_requires=">=3.11",
    # src/ holds only packages: web, web.app, worker, worker.etl and db.
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={"web.app": ["templates/*.html", "static/css/*.css"]},
    install_requires=[
        "Flask>=3.0",
        "psycopg[binary]>=3.2.4",   # 3.2.4+: Pylint 10/10 (older releases give false E1129)
        "SQLAlchemy>=2.0.18",       # 2.0.18+: create_engine rejects a non-numeric port
        "beautifulsoup4>=4.12",
        "lxml>=5.0",
    ],
    extras_require={
        "dev": ["pytest>=8.0", "pytest-cov>=5.0", "pylint>=4.0.6", "pydeps>=1.12.7"],
    },
    # Commands that work from any folder once the project is installed.
    entry_points={
        "console_scripts": [
            "gradcafe-load=db.load_data:main",
            "gradcafe-app-role=db.db_roles:main",
        ],
    },
)
