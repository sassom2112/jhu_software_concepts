Architecture
============

The service has three layers. Each has one job, and each talks only to the
layer below it:

.. code-block:: text

   Browser
     |  GET /analysis          POST /pull-data          POST /update-analysis
     v
   +-- Web layer (Flask) ------------------------------------------------------+
   |  webapp.create_app(scrape_fn, load_fn, query_fn)  - the application factory|
   |  webapp.routes    - the page and the two JSON button endpoints            |
   |  webapp.services  - PullState (the busy flag), default scrape/load        |
   |  templates/analysis.html - buttons, "Answer:" labels, results             |
   +------------+-----------------------------------------+--------------------+
                | SCRAPE_FN, LOAD_FN                      | QUERY_FN
                v                                         v
   +-- ETL layer -----------------------+   +-- Analysis ---------------------+
   |  scrape.py    -> raw entries       |   |  orm_queries.get_analysis()     |
   |  clean.py     -> records           |   |  query_data.py (same, raw SQL)  |
   |  load_data.py -> INSERT ...        |   |  analysis_common.py (rules,     |
   |     ON CONFLICT (p_id) DO NOTHING  |   |    wording, 2-decimal format)   |
   +----------------+-------------------+   +----------------+----------------+
                    | psycopg 3                              | SQLAlchemy 2
                    v                                        v
   +-- Database layer --------------------------------------------------------+
   |  PostgreSQL table "applicants" (p_id INTEGER PRIMARY KEY, ...)          |
   |  db_config.py - DATABASE_URL / PG* variables -> one connection setting   |
   |  models.py    - the Applicant ORM class mapped onto the same table       |
   +--------------------------------------------------------------------------+

Web layer
---------

``webapp.create_app()`` is an application factory. It takes the three pieces
of work the page depends on as keyword arguments:

* ``scrape_fn()``: fetch new raw entries. The default is
  ``webapp.services.default_scrape_fn``, which reads the stored ``p_id`` values
  and asks ``GradCafeScraper.scrape_new_entries`` for anything newer.
* ``load_fn(raw_entries)``: store them and return how many were new. The
  default is ``webapp.services.default_load_fn``, which runs ``clean_data``
  and then ``load_records``.
* ``query_fn()``: return everything the page shows. The default is
  ``orm_queries.get_analysis``.

Production passes nothing and gets the real implementations. Tests pass fakes,
which is how every button test runs without the network and, where it wants,
without a database (see :doc:`testing`).

``webapp.routes`` holds four routes: ``GET /`` redirects to ``GET /analysis``,
the page itself. ``POST /pull-data`` and ``POST /update-analysis`` return JSON,
so a *busy* answer (HTTP 409) can be shown without reloading the page.
``webapp.services.PullState`` is the busy flag both buttons check. See
:doc:`operations`.

ETL layer
---------

``scrape.py``
   ``GradCafeScraper`` fetches listing pages with ``urllib`` (honest user agent,
   ``robots.txt`` checked, a pause between pages) and parses them with
   BeautifulSoup into *raw entries*: the visible text of each row, plus the
   page's own JSON copy of the entry. ``scrape_new_entries`` is the Pull Data
   path. It walks newest-first and stops at the first page that contains an
   entry already stored. ``scrape_data`` is the resumable 30,000-entry Module 2
   run.

``clean.py``
   ``clean_data`` turns raw entries into records with typed fields (ISO dates,
   numbers, canonical statuses such as ``Waitlisted``) and keeps every raw
   string under ``raw`` so each value can be traced back.

``load_data.py``
   ``load_records`` maps records onto the table's columns and inserts them:
   ``COPY`` into a temporary staging table, then
   ``INSERT … ON CONFLICT (p_id) DO NOTHING``. Its callers run it inside one
   transaction, so a load either completes or changes nothing.
   ``fetch_applicants`` reads rows back as dictionaries keyed by the column
   names.

Database layer
--------------

The single table keeps the Module 3 schema unchanged:

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Column
     - Type
     - Content
   * - ``p_id``
     - ``INTEGER PRIMARY KEY``
     - Grad Café's result id (the number at the end of the entry's URL)
   * - ``program``
     - ``TEXT``
     - ``"Program, University"`` as listed on Grad Café
   * - ``comments``
     - ``TEXT``
     - the applicant's comment
   * - ``date_added``
     - ``DATE``
     - when the entry was posted
   * - ``url``
     - ``TEXT``
     - link to the entry
   * - ``status``
     - ``TEXT``
     - e.g. ``Accepted``, ``Rejected``, ``Waitlisted``, ``Interview``, ``Other``
   * - ``term``
     - ``TEXT``
     - e.g. ``Fall 2026``
   * - ``us_or_international``
     - ``TEXT``
     - ``American``, ``International`` or ``Other``
   * - ``gpa``, ``gre``, ``gre_v``, ``gre_aw``
     - ``FLOAT``
     - self-reported scores (averages use only values on the official scale)
   * - ``degree``
     - ``TEXT``
     - e.g. ``Masters``, ``PhD``
   * - ``llm_generated_program``, ``llm_generated_university``
     - ``TEXT``
     - names standardized by the Module 2 local LLM (``NULL`` for rows added
       by Pull Data until that step is run)

``db_config.py`` is the only place that knows how to reach the server.
``load_data.py`` and ``query_data.py`` connect with psycopg, and ``models.py``
builds a SQLAlchemy engine from the same settings. Both go through libpq, so
one ``DATABASE_URL`` (or ``~/.pgpass``) serves everything.

How a request flows
-------------------

**Opening the page.** ``GET /analysis`` calls ``QUERY_FN``, which opens one ORM
session, runs the eleven questions and formats every number with
``analysis_common``. Counts get thousands separators, and percentages and
averages get exactly two decimals. The template renders each result after an
``Answer:`` label. If the query raises, the page renders a banner instead of
a 500.

**Pull Data.** ``POST /pull-data`` first claims the busy flag
(``PullState.try_start``) and answers 409 if it is already taken. It then calls
``SCRAPE_FN`` and passes the result to ``LOAD_FN``. It always releases the flag
in a ``finally`` block and returns ``{"ok": true, "inserted": N}``, or a 500
with the error message. On success the browser shows how many rows were added
and then reloads the page. On a 409 or 500 it shows the message and leaves the
page as it is.

**Update Analysis.** ``POST /update-analysis`` does no database work. It answers
409 while a pull runs and 200 otherwise, and the browser then reloads the page.
Because the page recomputes on every request, that reload *is* the update.

Design decisions
----------------

* **Pull Data runs inside the request.** In Module 3 it was a separate
  background process, coordinated through a lock file. Module 4 fetches only
  entries newer than the newest stored one, which takes seconds to a few
  minutes, so the whole pipeline can run in-process. That makes it testable
  with plain fakes: no subprocess, no polling, no ``sleep()``.
* **Two query implementations, one set of rules.** ``query_data.py`` (SQL) and
  ``orm_queries.py`` (ORM) share the matching rules, valid score ranges,
  question wording and formatting in ``analysis_common.py``. A test runs both
  on the same data and requires identical output.
* **The data folder is the module folder.** The command-line tools read and
  write ``module_4/`` (``HERE`` in each module), and tests point ``HERE`` at a
  temporary folder, so running the tests never touches the committed data files.
