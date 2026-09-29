# Module 4 - Pytest and Sphinx

**Name:** Mike Sasso (JHED: msasso1)
**Course:** JHU EN.605.256 Modern Software Concepts in Python
**Module:** Module 4 - Testing and Documentation

| | |
| --- | --- |
| Documentation (Read the Docs) | https://sassom2112-jhu-software-concept.readthedocs.io/en/latest/ |
| Continuous integration | [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) · proof: [`actions_success.png`](actions_success.png) |
| Coverage proof | [`coverage_summary.txt`](coverage_summary.txt): 100% of `module_4/src` |
| Repository (SSH) | `git@github.com:sassom2112/jhu_software_concepts.git` (also in `github.txt`) |

Module 4 puts the Module 3 Grad Café application under test and documents it:

* a **pytest suite** of 260 tests with **100% line coverage** of `module_4/src`. Every test is marked,
  none touches the internet, none sleeps, and the database tests use a separate `*_test` database;
* a **GitHub Actions** workflow that starts PostgreSQL and runs the suite on every push that changes `module_4/`;
* **Sphinx documentation** (setup, architecture, API reference, testing guide, operational
  notes, troubleshooting), published on Read the Docs.

---

## 1. What is in this folder

```
module_4/
├── README.md                 # this file
├── requirements.txt          # exact versions: application + pytest + pytest-cov
├── pytest.ini                # markers + --cov=module_4/src --cov-fail-under=100
├── coverage_summary.txt      # the terminal coverage report of the full run
├── actions_success.png       # screenshot of a green GitHub Actions run
├── .github/workflows/tests.yml   # copy of the workflow GitHub runs (see section 5)
├── github.txt, .env.example  # SSH URL; names of the environment variables (no secrets)
│
├── src/                      # the application (Module 3 code, moved here)
│   ├── webapp/               #   Flask: create_app() factory, routes, services (busy flag), templates
│   ├── run.py                #   starts the web page
│   ├── scrape.py, clean.py   #   ETL: Grad Café scraper and cleaner (Module 2)
│   ├── load_data.py          #   ETL: PostgreSQL loader, INSERT ... ON CONFLICT (p_id) DO NOTHING
│   ├── db_config.py          #   DATABASE_URL / PG* variables -> connection
│   ├── models.py, orm_queries.py      # SQLAlchemy model and the 11 questions via the ORM
│   ├── query_data.py, analysis_common.py, build_query_results.py   # the same questions in SQL
├── tests/                    # ALL test code (section 4)
├── docs/                     # Sphinx: source/ (conf.py + pages), build/html/ (generated site)
│
│   Data and tools carried over from Module 3
├── llm_extend_applicant_data.json, applicant_data.json   # Module 2 cleaned data (~30,500 entries)
├── data/                     # raw_entries.json.gz, robots.txt (scraper evidence)
├── llm_hosting/              # the optional local-LLM standardizer (its own environment)
└── query_results.pdf, limitations.pdf, screenshots/, screenshot.jpg   # Module 3 deliverables
```

## 2. Setup

Python 3.11 (the application and tests also run on 3.10; building the docs needs 3.11) and PostgreSQL 13+ (developed on PostgreSQL 17 in Docker).

```bash
cd module_4
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

PostgreSQL (Docker, bound to localhost), plus the throwaway database the tests use:

```bash
docker run -d --name gradcafe-postgres --restart unless-stopped \
  -e POSTGRES_USER=gradcafe -e POSTGRES_DB=gradcafe -e POSTGRES_PASSWORD='choose-a-password' \
  -p 127.0.0.1:5432:5432 -v gradcafe_pgdata:/var/lib/postgresql/data postgres:17
docker exec gradcafe-postgres createdb -U gradcafe gradcafe_test
echo 'localhost:5432:*:gradcafe:choose-a-password' >> ~/.pgpass && chmod 600 ~/.pgpass
```

### Environment variables

Nothing secret is committed. libpq supplies the password from `~/.pgpass` (or `PGPASSWORD`), so it never
appears in code, URLs or commands.

| Variable | Meaning | Default |
| --- | --- | --- |
| `DATABASE_URL` | connection for every program, e.g. `postgresql://gradcafe@localhost:5432/gradcafe`; tests set it to a `*_test` database | unset → `PG*` below |
| `PGHOST` `PGPORT` `PGUSER` `PGDATABASE` | used when `DATABASE_URL` is unset | `localhost` `5432` `gradcafe` `gradcafe` |
| `FLASK_SECRET_KEY` | Flask session key | random per process |
| `FLASK_HOST` / `PORT` / `FLASK_DEBUG` | where `run.py` listens; `FLASK_DEBUG=1` for development only | `127.0.0.1` / `8080` / off |

## 3. Running the application

From `module_4`, with the virtual environment active:

```bash
export DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe
python src/load_data.py        # loads llm_extend_applicant_data.json; a second run inserts 0 rows
python src/run.py              # http://127.0.0.1:8080
```

* **Pull Data** scrapes Grad Café entries newer than the newest stored one (at most 50 pages, `robots.txt`
  checked, 2 s between pages, stops at the first block). It cleans them and inserts them, skipping ids
  already stored. It answers `{"ok": true, "inserted": N}`, or **409 `{"busy": true}`** if a pull is
  already running.
* **Update Analysis** refreshes the page, which re-queries PostgreSQL. It answers **409** while a pull runs.
* Other tools: `python src/query_data.py` (all 11 answers in SQL), `python src/orm_queries.py --all`
  (the same through SQLAlchemy), `python src/build_query_results.py` (writes `query_results.html`).
  The command-line tools read and write their files in `module_4/`, next to `src/`.

## 4. Tests

Run from the **repository root**, because `pytest.ini` measures `--cov=module_4/src`:

```bash
cd ..    # jhu_software_concepts/
DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test \
  module_4/.venv/bin/python -m pytest module_4 -m "web or buttons or analysis or db or integration"
```

Result: `260 passed`, `Required test coverage of 100% reached. Total coverage: 100.00%`, in about
2 seconds. `coverage_summary.txt` is this output, saved with `| tee module_4/coverage_summary.txt`.

| File | Marker | What it proves |
| --- | --- | --- |
| `test_flask_page.py` | web | the factory registers `/analysis`, `/pull-data`, `/update-analysis`; `GET /analysis` is 200 and shows both buttons, "Analysis" and "Answer:" |
| `test_buttons.py` | buttons | `POST /pull-data` is 200 and hands the scraper's rows to the loader; both endpoints return 409 `{"busy": true}` while a pull runs and do nothing; a failing load returns 500 and clears the busy flag |
| `test_analysis_format.py` | analysis | every result has an `Answer:` label; every percentage on the page has exactly two decimals (regex); formatter rounding |
| `test_db_insert.py` | db | table empty before, rows with the required non-null fields after `POST /pull-data`; no duplicates on a repeated or overlapping pull; `fetch_applicants` returns dicts with the Module 3 keys; schema and primary key; a failed load writes nothing |
| `test_integration_end_to_end.py` | integration | fake scraper → `POST /pull-data` → `POST /update-analysis` → `GET /analysis` shows the new, correctly formatted numbers; overlapping pulls stay unique |
| `test_app_wiring.py` | web, db | `/` redirect, database-down banner, `run.py`, the real `default_scrape_fn`, the ORM model |
| `test_query_tools.py` | analysis, db | SQL and ORM give identical answers on 300 synthetic rows; the three analysis command-line tools |
| `test_load_data.py`, `test_db_config.py`, `test_clean.py` | db | loader edge cases and command line, connection settings, every cleaning rule |
| `test_scrape_parsing.py`, `test_scrape_http.py`, `test_scrape_full.py` | db | the scraper against a fake Grad Café (`tests/fake_gradcafe.py`): URL safety, robots.txt, HTTP errors and retries, Pull Data, resumable full scrape, command line |

How the tests stay fast and deterministic:

* **Dependency injection:** `create_app(scrape_fn=..., load_fn=..., query_fn=...)` lets tests pass fakes
  (`FakeScraper`, `FakeLoader`, `FAKE_ANALYSIS` in `tests/conftest.py`).
* **Busy state without `sleep()`:** a test calls `app.pull_state.try_start()` and checks the 409s directly.
* **No internet:** the `fake_site` fixture replaces `urllib.request.urlopen` with `FakeSite` and records
  `time.sleep` calls instead of waiting. The scraper tests also pass with networking switched off.
* **Real database, safely:** the `database_url` fixture refuses any database whose name does not end in
  `_test`, and `db_conn` empties the table before each test.
* **Stable selectors:** `data-testid="pull-data-btn"`, `data-testid="update-analysis-btn"`, `.answer-label`,
  `role="alert"`.
* **Checked by mutation testing:** during development the scraper was broken on purpose, one small change at
  a time, in throwaway copies. Every change had to make a test fail; the weak tests found this way were
  strengthened.

The [testing guide](https://sassom2112-jhu-software-concept.readthedocs.io/en/latest/testing.html) lists every marker,
fixture and test double.

## 5. GitHub Actions

GitHub only runs workflows stored at the repository root, so the workflow that runs is
[`/.github/workflows/tests.yml`](../.github/workflows/tests.yml). `module_4/.github/workflows/tests.yml`
is an identical copy kept with the assignment, and CI fails if the two ever differ. On every push that
changes `module_4/` (or the workflow, or `.readthedocs.yaml`) the workflow:

1. starts a `postgres:17` service with a `gradcafe_test` database (`trust` authentication inside the
   throwaway container, so no password exists anywhere);
2. installs Python 3.11 and `requirements.txt`;
3. runs `pytest module_4 -m "web or buttons or analysis or db or integration"` from the repository root
   (fails below 100% coverage);
4. in a second job, builds the Sphinx docs with `-W` (warnings are errors).

`actions_success.png` shows a green run.

## 6. Documentation

Sphinx sources are in `docs/source`, and the generated HTML is committed in `docs/build/html`
(open `docs/build/html/index.html`). The published copy is on Read the Docs:
**https://sassom2112-jhu-software-concept.readthedocs.io/en/latest/**. It is built from `.readthedocs.yaml` at the
repository root, with warnings treated as errors.

| Page | Contents |
| --- | --- |
| Overview and setup | what the service does, install, PostgreSQL, environment variables, loading, running, tests |
| Architecture | web / ETL / database layers, schema, request flows, design decisions |
| Operational notes | busy-state policy, uniqueness key (`p_id`), idempotency strategy, scraper politeness, secrets |
| Troubleshooting | common local, CI and Read the Docs problems and their fixes |
| Testing guide | how to run, markers, selectors, fixtures and test doubles, CI |
| API reference | autodoc for `scrape`, `clean`, `load_data`, `query_data`, `webapp.routes` and the other modules |

Rebuild locally:

```bash
cd module_4
pip install -r docs/requirements.txt
sphinx-build -W -b html docs/source docs/build/html
```

## 7. Changes from Module 3

* **Layout:** the code moved into `src/`, and the tests are in `tests/`. The command-line tools still keep
  their data files in `module_4/` (`HERE` in each module), which is also what lets tests point them at a
  temporary folder.
* **Testability:** `create_app()` takes `scrape_fn`, `load_fn` and `query_fn`. Pull Data now runs in-process
  through `webapp/services.py` (Module 3 started a background process with a lock file). The busy flag is
  `PullState`, and both buttons return JSON (200/409/500) instead of redirecting. `load_data.fetch_applicants()`
  returns rows as dicts.
* **Bugs found by the tests and fixed:**
  * result tables had no `Answer:` label;
  * `build_query_results.py` crashed on an empty database;
  * the scraper fetched the first page twice when a "Next" link pointed back at it (the start URL was not
    normalized);
  * `scrape.py main()` left `scrape.log` open.
* **Other fixes:** Pull Data's result message no longer vanishes in an immediate reload (template JavaScript);
  unreachable code in `scrape_new_entries` was removed (found while reaching 100% coverage); the command-line
  tools find their data files in `module_4/` again after the move into `src/`.
* **Unchanged:** the `applicants` schema and its required fields, the eleven questions and their answers,
  and the scraper's politeness rules.

## 8. Known limitations

* The busy flag belongs to one Python process, which is right for `run.py`. A multi-worker server would
  need a shared lock (for example a PostgreSQL advisory lock).
* Rows added by Pull Data have empty LLM columns until the optional `llm_hosting` standardizer is run, so
  Question 11 and the LLM-field count of Question 9 do not include them yet.
* A Pull Data click waits for the scrape to finish: seconds normally, a few minutes for the 50-page maximum.
* The data are self-reported Grad Café submissions (see `limitations.pdf`).
