Grad Café Analytics
===================

A small data service built over Modules 2–4 of JHU EN.605.256 *Modern Software
Concepts in Python*. A scraper collects admissions results from
`The Grad Café <https://www.thegradcafe.com/survey/>`_, a cleaner turns them into
structured records, PostgreSQL stores them, and a Flask page answers eleven
questions about them. The page has two buttons: **Pull Data** fetches entries
that are newer than anything stored, and **Update Analysis** refreshes the
results.

Module 4 adds an automated pytest suite with 100% line coverage of
``module_4/src``, a GitHub Actions workflow that runs it against a real
PostgreSQL on every push that changes ``module_4``, and this documentation.

.. toctree::
   :maxdepth: 2
   :caption: Using the service

   overview
   architecture
   operations
   troubleshooting

.. toctree::
   :maxdepth: 2
   :caption: Developing

   testing
   api/index

Source code: https://github.com/sassom2112/jhu_software_concepts (folder ``module_4``).
