Web layer
=========

HTTP endpoints
--------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Request
     - Response
   * - ``GET /``
     - ``302`` redirect to ``/analysis``.
   * - ``GET /analysis``
     - ``200`` HTML page with every analysis result. If the database cannot be
       read, still ``200``, with an error banner (``role="alert"``) in place of
       the results.
   * - ``POST /pull-data``
     - ``200 {"ok": true, "inserted": N}`` after scraping and loading;
       ``409 {"busy": true}`` while another pull runs (nothing is done);
       ``500 {"ok": false, "error": "..."}`` if scraping or loading fails (the
       load is rolled back).
   * - ``POST /update-analysis``
     - ``200 {"ok": true}``; ``409 {"busy": true}`` while a pull runs.

Application factory
-------------------

.. automodule:: webapp
   :members: create_app

Routes
------

.. automodule:: webapp.routes

Services and the busy flag
--------------------------

.. automodule:: webapp.services

Start-up script
---------------

.. automodule:: run
   :no-members:
