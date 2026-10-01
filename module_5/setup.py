"""
setup.py - Makes module_5 an installable Python project.

    pip install -e .        (or:  uv pip install -e .)

installs the project in editable mode: the virtual environment points at src/
instead of holding a copy of it.  Every module (db_config, load_data, webapp,
...) then imports from any folder, and the gradcafe-load and gradcafe-app-role
commands below run from anywhere, without editing sys.path.  A change to the
code takes effect without reinstalling.  tests/conftest.py and
docs/source/conf.py still put src/ on sys.path themselves, so the test suite
and the Sphinx build also work where the project is not installed.

Keep the -e.  load_data, scrape, clean and build_query_results read and write
their data files in module_5/, the parent of src/.  A plain `pip install .`
copies the modules into the virtual environment, where they would look for
those files in vain.

requirements.txt pins the exact versions that were tested.  install_requires
below only names what the application needs at run time, with the oldest
versions it works with, so the package can also be installed next to newer
releases.  The "dev" extra adds the test and code-quality tools.
"""

from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"

setup(
    name="gradcafe-analysis",
    version="0.5.0",
    description="Grad Cafe admissions scraper, PostgreSQL loader and Flask analysis page "
                "(JHU EN.605.256 Modern Software Concepts in Python, Module 5)",
    long_description=(HERE / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Mike Sasso",
    python_requires=">=3.11",
    # src/ holds plain modules (db_config.py, load_data.py, ...) and one package, webapp.
    package_dir={"": "src"},
    py_modules=sorted(path.stem for path in SRC.glob("*.py")),
    packages=find_packages(where="src"),
    package_data={"webapp": ["templates/*.html", "static/css/*.css"]},
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
            "gradcafe-load=load_data:main",
            "gradcafe-app-role=db_roles:main",
        ],
    },
)
