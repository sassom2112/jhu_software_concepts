Operational notes
=================

Busy-state policy
-----------------

Only one pull may run at a time. The busy flag is
``webapp.services.PullState``, one per application instance:

* ``try_start()`` atomically flips *idle* to *running* under a lock. It
  returns ``False`` if a pull is already running.
* ``finish()`` flips it back. The route calls it in a ``finally`` block, so a
  scrape or load that crashes can never leave the app stuck *busy*.
* ``is_running`` is what the page and the buttons check.

While a pull is running:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Request
     - Answer
   * - ``POST /pull-data``
     - ``409 {"busy": true}``. Nothing is scraped or loaded.
   * - ``POST /update-analysis``
     - ``409 {"busy": true}``. The page tells the user that new data is still
       being retrieved.
   * - ``GET /analysis``
     - The page renders with the **Pull Data** button disabled.

When no pull is running, ``POST /update-analysis`` answers ``200 {"ok": true}``
and the page reloads. The page queries the database on every request, so
there is no cache to invalidate.

The flag is an in-memory object with no timer and no ``sleep()``, so tests can
set it directly (``app.pull_state.try_start()``) to check the busy path.

.. note::

   The flag belongs to one Python process. That is right for ``python src/run.py``
   and Flask's development server. Under a server that starts several worker
   processes, each worker would have its own flag. In that case, replace it
   with a lock every worker can see, for example a PostgreSQL advisory lock.

Uniqueness key
--------------

``applicants.p_id`` is the primary key and the only uniqueness rule. It is
Grad Café's own result id, the number at the end of each entry's URL
(``https://www.thegradcafe.com/result/1020461``). The site assigns it, so
the same entry always gets the same key, whether it arrives from the Module 2
bulk file, a Pull Data run, or a pull repeated by accident.

Idempotency strategy
--------------------

Loading the same data twice changes nothing:

1. Inside one batch, the first record with a given ``p_id`` wins, and later
   copies are dropped before anything reaches the database.
2. The batch is copied (``COPY``) into a temporary staging table, then inserted
   with ``INSERT … ON CONFLICT (p_id) DO NOTHING``. Rows that are already
   stored are skipped, and **existing rows are never updated**.
3. The load is a single transaction. If any row fails (for example, an id too
   large for ``INTEGER``), the whole batch is rolled back, Pull Data answers
   500, and no partial rows remain.
4. Pull Data also avoids re-fetching. ``scrape_new_entries`` receives the
   stored ids, walks the listing newest-first, and stops at the first page that
   contains one of them.

The tests pin each point down: a repeated pull inserts 0 rows, an overlapping
pull inserts only the new row, and a failed load leaves the table empty
(``tests/test_db_insert.py``, ``tests/test_integration_end_to_end.py``).

What a pull fetches, and how politely
--------------------------------------

* At most **50 pages** (about 1,000 entries) per click, newest first.
* ``robots.txt`` is fetched and checked at the start of every pull. Two
  independent evaluations must both allow the listing: Python's
  ``urllib.robotparser`` and an RFC 9309 longest-match check.
* **2 seconds** between pages, or longer if ``robots.txt`` asks for a larger
  ``Crawl-delay``. Every request has a 45-second timeout.
* A 401, 403 or 429, or a Cloudflare challenge page, **stops the pull at once**
  and is never retried or worked around. A plain 5xx is retried once after
  30 seconds. A timeout or connection error is retried after 30 and then
  120 seconds.
* The request only ever goes to ``www.thegradcafe.com``, and only to the
  listing, ``robots.txt`` and result pages. Every URL, including a "Next" link
  read from a page, is rebuilt from validated parts first.

A pull runs inside the HTTP request, so the browser waits for it: a few
seconds for a handful of new entries, and a few minutes for the full 50 pages.

Rows added by Pull Data
-----------------------

Pull Data cleans the new entries but does not run the Module 2 LLM
standardizer, which needs its own environment (``llm_hosting/``) and a local
model. New rows therefore have ``NULL`` in ``llm_generated_program`` and
``llm_generated_university``. Questions 9 and 11 are the only ones that use
those columns, and they leave such rows out until the standardizer has been
run over them.

Secrets and configuration
-------------------------

* No password, token or connection string is committed. The database password
  comes from ``~/.pgpass`` or ``PGPASSWORD`` and is read by libpq.
  ``FLASK_SECRET_KEY`` is optional; without it, each process gets a random key.
* Error messages about the database show ``user@host:port/dbname`` and never
  the password or the raw ``DATABASE_URL``.
* The CI database uses ``trust`` authentication inside the throwaway GitHub
  Actions container, so the workflow file contains no password either.

Protecting the real data from the tests
---------------------------------------

The database tests empty the ``applicants`` table before each test. The
``database_url`` fixture therefore refuses to run unless the database name
ends in ``_test``. A missing or mistyped ``DATABASE_URL`` makes those tests
*error* rather than wipe ``gradcafe``.
