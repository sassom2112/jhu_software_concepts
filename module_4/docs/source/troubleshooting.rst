Troubleshooting
===============

Running the tests locally
-------------------------

**pytest reports "collected 0 items".**
   pytest only collects files named ``test_*.py`` inside ``module_4/tests``.
   Check the file name (``smoketest.py`` is skipped; ``test_smoke.py`` is
   collected) and pass ``module_4`` as the path.

**"Coverage failure" or "module module_4/src was never imported" although tests pass.**
   The run started in the wrong folder. ``pytest.ini`` measures
   ``--cov=module_4/src`` relative to the directory pytest starts in. Run from
   the repository root, as in :doc:`testing`, not from inside ``module_4``.

**"refusing to run database tests against 'gradcafe'".**
   This is the safety guard working. The database tests empty the
   ``applicants`` table, so they run only against a database whose name ends
   in ``_test``. Put ``DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test``
   in front of the command, and create that database once with
   ``docker exec gradcafe-postgres createdb -U gradcafe gradcafe_test``.

**"database "gradcafe_test" does not exist".**
   Create it with the ``createdb`` command above.

**"password authentication failed" or "fe_sendauth: no password supplied".**
   libpq did not find the password. Add
   ``localhost:5432:*:gradcafe:YOUR_PASSWORD`` to ``~/.pgpass``. The ``*``
   covers both ``gradcafe`` and ``gradcafe_test``. Then run
   ``chmod 600 ~/.pgpass``, because libpq ignores the file if other users can
   read it. Or export ``PGPASSWORD`` for the current shell only.

**"connection failed ... Connection refused".**
   PostgreSQL is not running or not on that port. Start it with
   ``docker start gradcafe-postgres``, and check it with
   ``docker ps`` or ``pg_isready -h localhost``.

**"ModuleNotFoundError: No module named 'webapp'" (or 'scrape', 'load_data').**
   ``tests/conftest.py`` puts ``module_4/src`` on ``sys.path``. Make sure
   ``conftest.py`` is still in ``module_4/tests`` and has not been renamed.

**The editor underlines ``import webapp`` / ``import pytest`` as unresolved, but pytest runs fine.**
   The editor is using a different Python. In VS Code, run *Python: Select
   Interpreter*, choose ``module_4/.venv/bin/python``, and add
   ``module_4/src`` to ``python.analysis.extraPaths``.

**A single test passes alone but fails in the full run (or the other way round).**
   Some test shares state with another. The suite is written so that this
   never happens: every fixture is function-scoped except the read-only
   ``database_url``, files go under ``tmp_path``, and ``monkeypatch``
   restores everything. Look for a new test that writes outside ``tmp_path``
   or changes a module attribute without ``monkeypatch``.

Running the application
-----------------------

**The page shows "The analysis could not be loaded because the database is not reachable".**
   The page is working, but the query failed. The name in brackets (for
   example ``OperationalError``) says why. Check that PostgreSQL is running
   and that ``DATABASE_URL`` or the ``PG*`` variables point at the database
   that holds the data.

**Every count on the page is 0.**
   The table is empty. Load the Module 2 data with ``python src/load_data.py``
   (see :doc:`overview`).

**``python src/load_data.py`` says "cannot read input".**
   The input file is looked up by its bare name in the ``module_4`` folder.
   Check that ``llm_extend_applicant_data.json`` is there, or pass
   ``--file NAME`` with a file that is.

**Pull Data says "already running; this click did nothing".**
   Another pull is in progress, possibly started from another browser tab.
   That is the busy-state policy (see :doc:`operations`). Wait for it to
   finish.

**Pull Data fails with "robots.txt does not allow…" or "HTTP 403 … Cloudflare".**
   Grad Café refused the scraper, or could not confirm that it is allowed.
   The scraper never retries a block or tries to get around one. Try again
   later. Nothing was written.

**"Address already in use" when starting the page.**
   Something else is using port 8080. Start on another port with
   ``PORT=8081 python src/run.py``, or find the other process with
   ``ss -ltnp | grep 8080``.

GitHub Actions
--------------

**The workflow does not run at all.**
   GitHub runs only workflows under ``.github/workflows`` at the repository
   root. The copy in ``module_4/.github/workflows`` is the graded deliverable,
   and CI checks that the two are identical. The workflow is also limited to
   pushes that change ``module_4/``, the workflow file or
   ``.readthedocs.yaml``. To run it by hand, use *Run workflow* on the
   Actions tab.

**"refusing to run database tests" in CI.**
   The job's ``DATABASE_URL`` must name the service's ``gradcafe_test``
   database. The service container creates it through ``POSTGRES_DB``.

**Database tests fail with "Connection refused" in the first seconds of the job.**
   The service is not ready yet. The workflow's ``--health-cmd pg_isready``
   options make GitHub wait for it, so keep them if you edit the service.

**"The two copies of the workflow differ".**
   Copy the root workflow over the module copy
   (``cp .github/workflows/tests.yml module_4/.github/workflows/tests.yml``)
   and commit both.

**Coverage is 100% locally but not in CI.**
   A source file or test that exists only on your machine was never
   committed. ``git status`` shows it.

Read the Docs
-------------

**The build cannot import the modules for the API reference.**
   ``.readthedocs.yaml`` installs both ``module_4/requirements.txt`` (the
   application's own packages, which autodoc imports) and
   ``module_4/docs/requirements.txt`` (Sphinx and the theme).
   ``docs/source/conf.py`` puts ``module_4/src`` on ``sys.path``. Importing
   the modules needs no database: ``models.py`` creates its engine without
   connecting.

**The build fails on a warning.**
   Warnings are errors (``fail_on_warning: true``), to match
   ``sphinx-build -W`` locally and in CI. Run the local build to see the
   warning's file and line.

**Read the Docs cannot see the repository.**
   Read the Docs can only build from a public repository, or a private one on
   a paid plan. Make the GitHub repository public, then import it at
   https://app.readthedocs.org/dashboard/import/.
