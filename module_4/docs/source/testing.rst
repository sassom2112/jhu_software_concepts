Testing guide
=============

The suite lives in ``module_4/tests``. It covers every line of
``module_4/src``, runs in a few seconds, never touches the internet, and never
sleeps. Everything it needs from outside the process is replaced with a fake,
except PostgreSQL. For the database tests it uses a real, throwaway
``*_test`` database.

Running the suite
-----------------

Once, create the test database (see :doc:`overview` for the server itself):

.. code-block:: bash

   docker exec gradcafe-postgres createdb -U gradcafe gradcafe_test

Then, from the **repository root** (``jhu_software_concepts/``):

.. code-block:: bash

   DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test \
     module_4/.venv/bin/python -m pytest module_4 -m "web or buttons or analysis or db or integration"

The run must start at the root because ``module_4/pytest.ini`` adds
``--cov=module_4/src --cov-report=term-missing --cov-fail-under=100``. Coverage
paths are relative to the directory pytest starts in, and the run fails if any
line of ``src`` is not executed. The output ends with the coverage table and
``Required test coverage of 100% reached``. That output is committed as
``module_4/coverage_summary.txt``. To refresh it:

.. code-block:: bash

   DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test \
     module_4/.venv/bin/python -m pytest module_4 -m "web or buttons or analysis or db or integration" \
     | tee module_4/coverage_summary.txt

The password comes from ``~/.pgpass``, so it never appears in the command.

Markers
-------

Every test carries at least one of the five markers registered in
``pytest.ini``. The marker expression above therefore selects the entire
suite, and ``-m web`` (for example) selects one slice of it.

.. list-table::
   :header-rows: 1
   :widths: 14 46 40

   * - Marker
     - What it covers
     - Files
   * - ``web``
     - Routes are registered, the page loads and shows both buttons,
       ``/`` redirects, the database-down banner appears, and ``run.py``
       starts the server.
     - ``test_flask_page.py``, ``test_app_wiring.py``
   * - ``buttons``
     - ``POST /pull-data`` and ``POST /update-analysis``: 200 responses, the
       loader receives the scraper's rows, 409 while busy, 500 on a failed load.
     - ``test_buttons.py``
   * - ``analysis``
     - ``Answer:`` labels on every result, percentages with exactly two
       decimals, the number formatters, SQL and ORM giving identical
       answers, and the analysis command-line tools.
     - ``test_analysis_format.py``, ``test_query_tools.py``
   * - ``db``
     - The data layer: schema, inserts, duplicates and rollback in
       PostgreSQL, plus the code that feeds it (``db_config``,
       ``load_data``, ``clean``, ``scrape``).
     - ``test_db_insert.py``, ``test_load_data.py``, ``test_db_config.py``,
       ``test_clean.py``, ``test_scrape_parsing.py``, ``test_scrape_http.py``,
       ``test_scrape_full.py``, and part of ``test_app_wiring.py``
   * - ``integration``
     - End to end: pull, then update, then render, with overlapping pulls
       and a pull that is still running.
     - ``test_integration_end_to_end.py``

The assignment fixes these five names, so the scraper and cleaner tests use
``db``, the marker for the data layer, even though most of them need no
database. ``test_query_tools.py`` carries both ``analysis`` and ``db``.

Which tests need PostgreSQL
^^^^^^^^^^^^^^^^^^^^^^^^^^^

A test needs the database only if it asks for ``db_conn``, directly or through
``db_client``, ``db_app`` or ``sample_data``. Without a ``DATABASE_URL`` that
names a ``*_test`` database, those tests **error** at the guard, and every other
test still passes. For a quick run with no database at all:

.. code-block:: bash

   module_4/.venv/bin/python -m pytest module_4 -m "web or buttons" --no-cov

Stable selectors
----------------

The tests read the page with BeautifulSoup, through selectors that exist for
that purpose and do not depend on styling:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Selector
     - Element
   * - ``[data-testid="pull-data-btn"]``
     - The Pull Data button. It has the ``disabled`` attribute while a pull is
       running.
   * - ``[data-testid="update-analysis-btn"]``
     - The Update Analysis button.
   * - ``.answer-label``
     - The ``Answer:`` label before every result line, and above every result
       table.
   * - ``article.card``, ``.card-number``, ``div.result`` (``dt`` / ``dd``)
     - One question card, its number, and each labelled result inside it.
   * - ``.stat-value``
     - The summary numbers at the top (the first one is the entry count).
   * - ``[role="alert"]``
     - The banner shown when the database cannot be read.

Percentages are checked with two regular expressions, after ``<script>`` and
``<style>`` are removed from the page. ``(\d[\d,]*(?:\.\d+)?)\s*%`` finds every
percentage, and ``^\d[\d,]*\.\d{2}$`` requires exactly two decimals.

Fixtures and test doubles
-------------------------

Shared fixtures live in ``tests/conftest.py``. pytest finds that file on its
own, so no test imports it.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Fixture
     - What it provides
   * - ``fake_scraper``
     - A ``FakeScraper``: returns ``.rows`` (empty until a test sets it) and
       counts ``.calls``. It replaces ``SCRAPE_FN``.
   * - ``fake_loader``
     - A ``FakeLoader``: records every batch in ``.calls`` and "inserts"
       ``len(rows)``. It replaces ``LOAD_FN``.
   * - ``app`` / ``client``
     - ``create_app(scrape_fn=fake_scraper, load_fn=fake_loader, query_fn=…)``
       returning ``FAKE_ANALYSIS``, and Flask's in-process test client for it.
       No network and no database.
   * - ``raw_entries`` / ``entry_factory``
     - Three raw entries shaped exactly like the scraper's output, and
       ``make_raw_entry(result_id, **overrides)`` for tests that need specific
       values.
   * - ``database_url``
     - Session-scoped guard. It fails the test unless the database name ends
       in ``_test``.
   * - ``db_conn``
     - An autocommit psycopg connection to the test database, with the
       ``applicants`` table created and **emptied** before each test.
   * - ``db_app`` / ``db_client``
     - The real loader and real ORM queries against the test database. Only
       the scraper is fake.
   * - ``fake_site``
     - Replaces the internet for one test. ``urllib.request.urlopen`` is
       answered by ``fake_gradcafe.FakeSite``, and ``time.sleep`` is recorded
       in ``fake_site.sleeps`` instead of waiting.
   * - ``scraper``
     - A ``GradCafeScraper`` whose data folder is the test's ``tmp_path``.

``tests/fake_gradcafe.py`` is not a test file; it is the toolkit the scraper
tests import:

* ``listing_page(applicants, next_url=…, payload=…)`` builds a survey page
  with the same structure as the real one: the results table, badge and
  comment rows, the mobile-only duplicate badge, ad rows, the "Next" link,
  and the JSON copy of the rows. ``applicant(result_id, **fields)`` describes
  one row.
* ``make_cursor(...)`` and ``survey_url(cursor)`` build the cursor-paginated
  URLs exactly as the scraper requests them.
* ``FakeSite.serve(url, *responses)`` queues responses for one URL:
  ``page(html, headers)``, ``http_error(code, body, headers)`` (a fresh
  ``HTTPError`` every time), or any exception instance such as
  ``URLError(...)``. ``serve_listing([[ids], [ids], ...])`` serves
  ``robots.txt`` and a chain of pages joined by "Next" links. The site
  records ``requested``, ``user_agents``, ``timeouts`` and ``sleeps``.

Some files add their own fixtures on top: ``mixed_client``
(``test_analysis_format.py``), ``first_batch``
(``test_integration_end_to_end.py``), ``sample_data`` and ``report_path``
(``test_query_tools.py``), and ``input_dir``, ``work_dir`` and ``module_dir``.
Those last three point ``load_data``, ``clean`` and ``scrape`` at ``tmp_path``
through ``monkeypatch.setattr(module, "HERE", tmp_path)``, so command-line tests
never write into the real module folder.

Techniques used
---------------

* **Dependency injection.** ``create_app(scrape_fn=, load_fn=, query_fn=)``
  lets every button test run the real routes against fakes.
* **Busy state without waiting.** ``app.pull_state.try_start()`` puts the app
  in the *pull running* state directly. The test then checks the 409s, and
  ``finish()`` ends it.
* **No network.** Every scraper test that would make a request uses
  ``fake_site``, and the parsing tests make none. The scraper tests also pass
  inside a network namespace with no network at all (``unshare -rn``).
* **monkeypatch / runpy / capsys / caplog / tmp_path.**
  ``monkeypatch.setattr`` / ``setenv`` swap a class, a constant or an
  environment variable for one test. ``runpy.run_module(name,
  run_name="__main__")`` runs a module exactly as ``python name.py`` would,
  which covers its ``if __name__ == "__main__"`` line. ``capsys`` and
  ``caplog`` capture printed output and log records. ``tmp_path`` is a fresh
  folder per test.
* **Red, then green.** Two real bugs were found by writing the test first.
  The table cards had no ``Answer:`` label, and ``build_query_results.py``
  crashed on an empty database. Each test failed before the fix and passes
  after it.
* **Mutation testing.** During development the scraper tests were checked by
  breaking ``scrape.py`` on purpose, one small change at a time, in throwaway
  copies. Every change had to make at least one test fail, and the tests that
  let a change through were strengthened.

Continuous integration
----------------------

``.github/workflows/tests.yml`` (at the repository root, where GitHub runs it,
with an identical copy at ``module_4/.github/workflows/tests.yml``) runs on
every push that changes ``module_4`` (or the workflow file, or ``.readthedocs.yaml``):

1. It starts a ``postgres:17`` service container with a ``gradcafe_test``
   database and ``trust`` authentication, so no password is needed or stored.
2. It checks that the two copies of the workflow are identical.
3. It installs Python 3.11 and ``module_4/requirements.txt``.
4. It runs the same pytest command as above, from the repository root, with
   ``DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test``, and
   fails below 100% coverage.
5. In a second job, it builds this documentation with ``sphinx-build -W``.

Adding a test
-------------

* Name the file ``test_*.py`` (pytest also accepts ``*_test.py``) and put
  it in ``module_4/tests``, where ``conftest.py`` provides the fixtures.
* Mark every test with one of the five markers, or set ``pytestmark`` for the
  whole file.
* Use ``client`` for the page and buttons, ``db_conn`` / ``db_client`` for
  anything that must reach PostgreSQL, and ``fake_site`` for anything that
  would reach Grad Café.
* Never call ``time.sleep``, never depend on the clock or on test order, and
  write files only under ``tmp_path``.
