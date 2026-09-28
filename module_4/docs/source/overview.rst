Overview and setup
==================

What the service does
---------------------

* **Collects** admissions results from The Grad Café's public survey listing
  (``src/scrape.py``), politely: it checks ``robots.txt``, waits between pages
  and stops at the first sign of a block.
* **Cleans** each raw listing into a structured record: program, university,
  degree, term, decision, GPA and GRE scores (``src/clean.py``).
* **Stores** the records in one PostgreSQL table, ``applicants``, keyed by
  Grad Café's own result id, so loading the same entry twice never creates a
  duplicate (``src/load_data.py``).
* **Answers** eleven questions about the data, twice: in handwritten SQL
  (``src/query_data.py``) and through the SQLAlchemy ORM
  (``src/orm_queries.py``). A test holds the two to identical answers.
* **Serves** the answers on a Flask page (``src/webapp``). Its **Pull Data**
  button fetches and stores entries newer than anything already stored, and
  **Update Analysis** refreshes the page. Neither does anything while a pull is
  already running.

Requirements
------------

* Python 3.11 (3.10 or newer works). The CI runs 3.11.
* PostgreSQL 13 or newer. Development used PostgreSQL 17 in Docker.
* Linux, macOS or WSL. The commands below use a POSIX shell.

Install
-------

.. code-block:: bash

   git clone https://github.com/sassom2112/jhu_software_concepts.git
   cd jhu_software_concepts/module_4
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

Unless a section says otherwise, the commands on this page run from the
``module_4`` folder with the virtual environment active.

PostgreSQL
----------

Any PostgreSQL server works. With Docker, one command starts a server that
listens on ``localhost`` only:

.. code-block:: bash

   docker run -d --name gradcafe-postgres --restart unless-stopped \
     -e POSTGRES_USER=gradcafe -e POSTGRES_DB=gradcafe -e POSTGRES_PASSWORD='choose-a-password' \
     -p 127.0.0.1:5432:5432 -v gradcafe_pgdata:/var/lib/postgresql/data postgres:17

The tests need a second, throwaway database whose name ends in ``_test``. See
:doc:`testing` for why.

.. code-block:: bash

   docker exec gradcafe-postgres createdb -U gradcafe gradcafe_test

Environment variables
---------------------

No password or connection string is stored in the repository. Every program,
whether psycopg or SQLAlchemy, reads the connection from the environment
through ``db_config.get_database_url()``. The table below lists every variable
the code reads. ``.env.example`` in the module folder shows the same names.

.. list-table::
   :header-rows: 1
   :widths: 22 48 30

   * - Variable
     - Meaning
     - Default
   * - ``DATABASE_URL``
     - The PostgreSQL connection, as a URL (``postgresql://gradcafe@localhost:5432/gradcafe``)
       or a libpq ``key=value`` string. The SQLAlchemy spelling
       ``postgresql+psycopg://…`` is accepted too. Tests override it to point
       at a ``*_test`` database.
     - unset: the ``PG*`` variables below are used
   * - ``PGHOST`` / ``PGPORT`` / ``PGUSER`` / ``PGDATABASE``
     - Standard libpq settings, used when ``DATABASE_URL`` is unset.
     - ``localhost`` / ``5432`` / ``gradcafe`` / ``gradcafe``
   * - ``PGPASSWORD`` or ``~/.pgpass``
     - The password. libpq reads it itself, so it never appears in code,
       URLs or shell history. Prefer ``~/.pgpass`` (see below).
     - none
   * - ``FLASK_SECRET_KEY``
     - Flask's session key.
     - a random key per process
   * - ``FLASK_HOST`` / ``PORT``
     - Address ``src/run.py`` listens on.
     - ``127.0.0.1`` / ``8080``
   * - ``FLASK_DEBUG``
     - ``1`` turns on Flask's debugger (development machines only).
     - off

Store the password once, readable only by you:

.. code-block:: bash

   echo 'localhost:5432:*:gradcafe:choose-a-password' >> ~/.pgpass
   chmod 600 ~/.pgpass

Load the data
-------------

``llm_extend_applicant_data.json`` (in the module folder) holds about 30,500
cleaned entries from Module 2, including the LLM-standardized program and
university names. Loading it creates the table if needed and skips any entry
that is already stored:

.. code-block:: bash

   export DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe
   python src/load_data.py

A second run reports ``Inserted 0 new rows``. ``--file NAME`` loads another
file from the module folder, and ``--reset`` drops and recreates the table
first.

Run the web page
----------------

.. code-block:: bash

   python src/run.py

Open http://127.0.0.1:8080. It redirects to ``/analysis``, which reads the
database on every request. If PostgreSQL is not reachable, the page still
loads and shows a red banner that says so.

Other command-line tools
------------------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Command
     - What it does
   * - ``python src/query_data.py``
     - Prints all eleven answers, computed with handwritten SQL.
   * - ``python src/orm_queries.py [--all]``
     - Prints the required ORM questions (1, 4, 5, 8, 9, 10), or all eleven.
   * - ``python src/build_query_results.py``
     - Writes ``query_results.html`` (question, result, SQL, explanation),
       which a browser can print to PDF.
   * - ``python src/scrape.py --help``
     - Lists the options of the Module 2 scraper. A real run contacts the
       live site and **rewrites** ``data/raw_entries.json(.gz)`` and, unless
       ``--no-clean`` is given, ``applicant_data.json``.
   * - ``python src/clean.py``
     - Cleans ``data/raw_entries.json`` (or its ``.gz`` copy) into
       ``applicant_data.json``.

The command-line tools read and write their files in the ``module_4`` folder,
next to ``src/`` rather than inside it. File options take a bare name, and
anything that looks like a path is reduced to its last part.

Run the tests
-------------

The suite runs from the **repository root**, because ``pytest.ini`` measures
coverage of ``module_4/src``:

.. code-block:: bash

   cd ..    # the repository root, jhu_software_concepts/
   DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test \
     module_4/.venv/bin/python -m pytest module_4 -m "web or buttons or analysis or db or integration"

:doc:`testing` explains the markers, the fixtures and the fakes.

Build this documentation
------------------------

.. code-block:: bash

   pip install -r docs/requirements.txt
   sphinx-build -W -b html docs/source docs/build/html

``-W`` turns every warning into an error, as the Read the Docs build does.
The HTML lands in ``docs/build/html`` (open ``index.html``).
