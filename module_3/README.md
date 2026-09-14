# Module 3 - Database Queries, SQLAlchemy, and Dynamic Webpages

**Name:** Mike Sasso (JHED: msasso1)
**Course:** JHU EN.605.256 Modern Software Concepts in Python
**Module:** Module 3 - Database Queries, SQLAlchemy, and Dynamic Webpages
**Due:** September 20, 2026

---

## 1. What is in this folder

```
module_3/
├── README.md                     # this file
├── requirements.txt              # exact package versions of the module_3 environment
├── github.txt                    # SSH URL of the private GitHub repository
├── .env.example                  # names of the environment variables (no secrets)
│
├── db_config.py                  # builds the PostgreSQL connection from environment variables
├── load_data.py                  # Part 1: create the applicants table and load the Module 2 data (psycopg 3)
├── analysis_common.py            # matching rules, valid score ranges, question text and number formatting
├── query_data.py                 # Part 2/3: all 11 questions in handwritten SQL (psycopg 3)
├── models.py                     # Part 5: SQLAlchemy 2.x Applicant model, Engine and Session
├── orm_queries.py                # Part 6: the questions again through the ORM (no handwritten SQL)
├── build_query_results.py        # Part 4: builds query_results.html -> query_results.pdf
├── query_results.pdf             # Part 4: question, result, SQL and explanation for all 11 questions
├── limitations.pdf               # Part 11: written reflection on self-reported data
├── pull_data.py                  # Part 9: fetch new Grad Café entries -> clean -> LLM -> PostgreSQL
├── run.py                        # Part 8: starts the Flask web page
├── webapp/
│   ├── __init__.py               # Flask application factory
│   ├── routes.py                 # analysis page, Pull Data, Update Analysis, status endpoint
│   ├── pull_manager.py           # starts pull_data.py in the background; one run at a time
│   ├── templates/                # base.html, analysis.html
│   └── static/css/style.css
├── screenshots/                  # raw SQL console, ORM console, running Flask page
│
│   Carried over from Module 2 (used by load_data.py and Pull Data)
├── scrape.py                     # Module 2 scraper + new scrape_new_entries() for Pull Data
├── clean.py                      # Module 2 cleaner
├── applicant_data.json           # Module 2 cleaned data (30,500 entries)
├── llm_extend_applicant_data.json# Module 2 cleaned data + LLM columns (what load_data.py loads)
├── data/raw_entries.json.gz, data/robots.txt
├── screenshot.jpg                # Module 2 robots.txt evidence
└── llm_hosting/                  # Module 2 local LLM standardizer (its own environment)
```

## 2. Setup

Python 3.10 or newer (developed on 3.11).

```bash
cd module_3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.1 PostgreSQL

Any PostgreSQL 13+ works. The development machine ran PostgreSQL 17 in Docker,
bound to localhost only:

```bash
docker run -d --name gradcafe-postgres --restart unless-stopped \
  -e POSTGRES_USER=gradcafe -e POSTGRES_DB=gradcafe -e POSTGRES_PASSWORD='choose-a-password' \
  -p 127.0.0.1:5432:5432 -v gradcafe_pgdata:/var/lib/postgresql/data postgres:17
```

A local installation (`sudo apt install postgresql`, then `createuser` /
`createdb`) works the same way.

### 2.2 Credentials (nothing secret is committed)

All programs read the connection from environment variables
(see `db_config.py` and `.env.example`):

* `DATABASE_URL`, a URL such as `postgresql://gradcafe@localhost:5432/gradcafe`
  (a libpq `key=value` string such as `host=localhost dbname=gradcafe` also works), or
* the standard `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE` (defaults: `localhost`, `5432`, `gradcafe`, `gradcafe`).

The password is supplied by libpq itself, so it never appears in code, URLs
or the repository. Either store it in `~/.pgpass`:

```bash
echo 'localhost:5432:*:gradcafe:choose-a-password' >> ~/.pgpass && chmod 600 ~/.pgpass
```

or export `PGPASSWORD` in the current shell only. psycopg and SQLAlchemy's
psycopg driver both use libpq, so one setting serves every program.

### 2.3 LLM environment for Pull Data (optional)

Pull Data standardizes new entries with the Module 2 local LLM, which has its
own environment because `llama-cpp-python` compiles native code (about two
minutes with gcc):

```bash
cd llm_hosting
python3 -m venv .venv
.venv/bin/pip install --no-binary llama-cpp-python -r requirements.txt
cd ..
```

The TinyLlama model (about 670 MB) downloads into `llm_hosting/models/` the
first time it is needed. Without this environment Pull Data still adds the new
rows, but their `llm_generated_program` and `llm_generated_university` are left
empty, so Questions 9 and 11 do not count them. The status message says so.

## 3. Running everything

| Step | Command | What it does |
| --- | --- | --- |
| Load | `python load_data.py` | creates `applicants` if needed and inserts `llm_extend_applicant_data.json` |
| Raw SQL | `python query_data.py` | prints all 11 answers computed with SQL |
| ORM | `python orm_queries.py` | prints Questions 1, 4, 5, 8, 9 and original Question 10 via SQLAlchemy (`--all` prints all 11) |
| PDF | `python build_query_results.py` | writes `query_results.html`; print it to `query_results.pdf` |
| Web page | `python run.py` | serves the analysis at http://127.0.0.1:8080 |
| Pull by hand | `python pull_data.py` | the same pipeline the Pull Data button starts |

`load_data.py` options: `--file NAME` loads another JSON or JSON.gz file from
this folder (the plain `applicant_data.json` works too; its LLM columns are
then empty), and `--reset` drops and recreates the table first.

## 4. Part 1: loading the data (`load_data.py`)

* **Schema.** The table matches the assignment exactly: `p_id INTEGER PRIMARY KEY`,
  `date_added DATE`, the four scores `FLOAT`, and every other column `TEXT`.
* **p_id** is Grad Café's own result id, the number at the end of each entry's
  URL. It is unique and stable, so the same entry always gets the same key.
* **program** holds Module 2's combined `"Program, University"` text, the
  original downloaded wording. For the 8 entries whose program name is blank on
  Grad Café, it falls back to the university.
* **No duplicates, no overwrites.** Rows are bulk-copied (`COPY`) into a
  temporary staging table, then inserted with `INSERT ... ON CONFLICT (p_id) DO NOTHING`.
  A second run reports `Inserted 0 new rows; 30,500 were already present`.
  Existing rows are never modified.
* **Missing and messy values.** Missing values become `NULL`, unparseable or
  non-finite numbers become `NULL` rather than aborting, blank strings become
  `NULL`, and NUL characters (which PostgreSQL text cannot store) are removed.
* **Error handling.** The whole load is one transaction. Invalid connection
  settings or an unreachable server exit with a clear message and code 2. Neither
  message ever prints a password or the raw setting, and connecting gives up
  after 10 seconds. Any SQL error during the load rolls everything back
  (code 3).

## 5. Parts 2 and 3: SQL analysis (`query_data.py`)

All analysis is expressed in SQL (`COUNT ... FILTER`, `AVG`, `ROUND(...::numeric, 2)`,
`GROUP BY` / `HAVING`, PostgreSQL regular expressions). Python only sends each
query and formats the numbers. The 11 questions, their results, the SQL and an
explanation of each are in `query_results.pdf`.

Conventions, all defined once in `analysis_common.py` and shared with the ORM:

* **Text matching** ignores capitalization and surrounding spaces
  (`LOWER(TRIM(term)) = 'fall 2026'`). An acceptance is a status starting with
  "accept".
* **Nationality (Q2).** The denominator is the entries classified International,
  American or Other. Missing and blank values are excluded, and only
  International counts in the numerator.
* **Universities and programs (Q7, Q8)** are recognized in the original
  `program` text with case-insensitive regular expressions: Johns Hopkins, John
  Hopkins or the word JHU; Georgetown; Massachusetts Institute of Technology or
  the case-sensitive acronym MIT; Stanford; Carnegie Mellon or CMU. Computer
  Science also matches "Electrical Engineering and Computer Science" and EECS,
  because that is how MIT lists its CS PhD.
* **Degrees.** A master's degree starts with "Master"; a PhD matches PhD or Ph.D.
* **Q9** keeps Q8's term, status and degree filters and swaps in
  `llm_generated_program` and `llm_generated_university`. The university must
  equal one of the four canonical names.
* **Averages** include every applicant who reports that metric, independently of
  the others, but **only values on the official scale**: GPA above 0 and at most
  4.0, GRE Verbal and Quantitative 130–170, Analytical Writing 0–6. This matters
  because applicants often type their 260–340 GRE total into the Quantitative
  field (1,427 of the 2,426 values), or 99.99 into Analytical Writing. Averaging
  the column as stored gives a "GRE Quantitative" of about 260. The stored values
  are never changed.
* **Formatting.** Counts are whole numbers, and percentages and averages have two
  decimals. Rounding happens in SQL with `ROUND` on `numeric` (half up), and the
  Python formatter uses the same rule.

**Why Q8 and Q9 agree.** On the loaded data both counts are 32. Grad Café's
current submission form has applicants choose the university from a list, so
these four schools arrive with one spelling each. The LLM step only strips the
"(MIT)" suffix, and the Computer Science program names are consistent, so both
field sets select the same entries. The counts would differ for free-text
entries: an abbreviated or misspelled school or program is caught by the LLM
fields only when the standardizer maps it correctly, and a wrong LLM mapping
can add or drop an entry that the original text classifies correctly.

**Original questions.**
* **Q10:** for Fall 2026, how do the acceptance rate and the average GPA of
  accepted applicants compare across degree types with at least 100 entries?
  (grouping, filtering, percentages, averages)
* **Q11:** which ten universities, by LLM-standardized name, have the most
  Fall 2026 entries, and what share of each school's entries report an
  acceptance? (grouping over the standardized names, ordering, percentages)

## 6. Parts 5 and 6: SQLAlchemy ORM (`models.py`, `orm_queries.py`)

* `models.py` defines `Base(DeclarativeBase)` and `Applicant`, mapped to the
  existing `applicants` table with SQLAlchemy 2.x typed columns
  (`Mapped[...]`, `mapped_column`). `p_id` is the primary key. The engine
  is `create_engine(get_sqlalchemy_url(), pool_pre_ping=True)` using the
  psycopg 3 driver, and `SessionLocal = sessionmaker(...)` is used as a
  context manager. There is no `create_all()`, so no second copy of the data
  is ever created.
* `orm_queries.py` builds every query from the model with `select()`,
  `where()`, `func.count()`, `func.avg()`, `.filter()`, `and_()`, `or_()`,
  `group_by()` and `having()`. It uses no `text()` and no database cursor.
* Equivalent questions give identical answers. All 11 were compared
  programmatically, SQL against ORM, and also recomputed independently from the
  JSON file in plain Python.
* The Flask page reads every result through `orm_queries.get_analysis()`.

## 7. Part 7: raw SQL vs. SQLAlchemy (Question 8)

Raw SQL (`query_data.py`):

```sql
SELECT COUNT(*) AS accepted_cs_phd_entries
FROM applicants
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND TRIM(status) ILIKE 'accept%'
  AND degree  ~* '^\s*ph\.?\s*d'
  AND program ~* '(computer\s+science|\meecs\M)'
  AND (   program ~* 'georgetown'
       OR program ~* 'massachusetts\s+institute\s+of\s+technology'
       OR program ~  '\mMIT\M'
       OR program ~* 'stanford'
       OR program ~* '(carnegie\s+mellon|\mcmu\M)');
```

SQLAlchemy (`orm_queries.py`):

```python
def _q8_base_conditions():
    return and_(_is_term(rules.FALL_2026), _is_accepted(), _is_phd())

def _original_target_university():
    patterns = [Applicant.program.op("~*")(regex) for regex in rules.ORIGINAL_UNIVERSITY_REGEXES]
    patterns.append(Applicant.program.op("~")(rules.MIT_ACRONYM_REGEX))
    return or_(*patterns)

def q8_accepted_cs_phd_original(session: Session) -> int:
    stmt = select(func.count()).select_from(Applicant).where(
        and_(_q8_base_conditions(), _mentions_computer_science(Applicant.program), _original_target_university())
    )
    return session.scalar(stmt) or 0
```

The raw SQL is easier to read top to bottom and easier to debug: it can be
pasted into `psql` unchanged, and it shows exactly what PostgreSQL receives,
which matters for PostgreSQL-specific features like `~*`, `FILTER` and
`ROUND(...::numeric, 2)`. The ORM version is more abstract but composable.
Conditions such as "Fall 2026 and accepted and PhD" are ordinary Python
functions, reused unchanged by Questions 8 and 9 and the original questions, so
a rule changes in one place instead of in every query string. The ORM also
references columns through the `Applicant` model, so a typo in a column name
fails in Python rather than as a runtime SQL error, and values are passed as
bound parameters instead of pasted into strings. Its disadvantages here are
that the generated SQL has to be printed to be checked, and regular-expression
matching needs the dialect-specific `.op("~*")`, so this code is no more
portable than the SQL.

## 8. Parts 8–10: the web page

Start it with `python run.py` and open http://127.0.0.1:8080.

* **Analysis page.** On every request it reads PostgreSQL through the ORM and
  shows the entry count, the newest entry date, all nine required questions and
  the two original questions, styled with `webapp/static/css/style.css`.
* **Pull Data** starts `pull_data.py` as a separate process
  (`subprocess.Popen`, its own session) and returns immediately. The pipeline:
  1. read the `p_id`s already stored;
  2. `GradCafeScraper.scrape_new_entries()` (new in this module's copy of
     `scrape.py`) walks the newest listing pages and stops at the first page
     containing an already-stored entry, with robots.txt, 2 s delays and
     stop-on-block unchanged from Module 2. A run fetches at most 50 pages
     (about 1,000 entries). If it reaches that limit first, the position is
     saved in `data/pull_resume.json`, the message says more entries remain, and
     the next run continues from there, so no gap of missing entries is left;
  3. `clean.clean_data()`;
  4. `llm_hosting/run_parallel.py` adds the LLM columns (answers are cached,
     so only new program strings reach the model); a step that runs longer than
     30 minutes is stopped and the rows are stored without LLM names;
  5. `load_records()` inserts with `ON CONFLICT (p_id) DO NOTHING`.

  Progress and the outcome go to `data/pull_status.json`, which the page polls
  every few seconds.
* **One run at a time.** `pull_data.py` holds an exclusive OS file lock
  (`fcntl.flock` on `data/pull_data.lock`) for its whole run, and the web app
  also guards the start with a thread lock. Clicking Pull Data again while a
  run is active shows "Pull Data is already running..." and starts nothing.
  The OS releases the lock if a run crashes, and the page then reports that the
  last run stopped unexpectedly.
* **Update Analysis** (top right) redirects to the page, which re-queries the
  database; it never starts a scrape. During a pull it says that new data is
  currently being retrieved and shows the results saved so far. Otherwise it
  confirms the refresh.
* **Clear failures.** If Grad Café blocks or rate-limits the scraper, or the
  network or database fails, the page shows a plain-language message and no
  data is changed. If the LLM environment (section 2.3) is missing, new rows
  are stored with empty LLM columns and the message says so. If PostgreSQL is
  down, the page shows only that error, never a contradictory success message.

## 9. Screenshots

`screenshots/` contains:

* `sql_console_output.png`: the output of `python query_data.py` (raw SQL).
* `orm_console_output.png`: the output of `python orm_queries.py` (SQLAlchemy ORM).
* `flask_webpage.png`: the running page at http://127.0.0.1:8080, captured with headless Chrome.

The two console images were rendered into a terminal-style image from the
programs' captured output of a run on 2026-09-14. The numbers are exactly
what the programs printed.

## 10. Known limitations

* The analysis describes Grad Café submissions, not all applicants; see
  `limitations.pdf`.
* The single-run lock uses `fcntl`, which exists on Linux, macOS and WSL. On
  native Windows the lock check is skipped and only the in-process guard
  prevents a second pull.
* Pull Data needs Grad Café to accept the scraper's requests. On
  2026-09-12 Cloudflare briefly challenged it, and a pull during such a period
  stops immediately with a message instead of retrying or working around the
  block.
* The LLM step needs the `llm_hosting` environment and its model (section 2.3).
