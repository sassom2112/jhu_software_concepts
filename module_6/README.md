# Module 6 - Deploy Anywhere: Docker Compose, RabbitMQ and a Background Worker

**Name:** Mike Sasso (JHED: msasso1)
**Course:** JHU EN.605.256 Modern Software Concepts in Python
**Module:** Module 6 - Deploy Anywhere (containers, Docker Compose, RabbitMQ)

| | |
| --- | --- |
| Run it | `cp .env.example .env`, replace every `change-me`, then `docker compose up --build` in `module_6/` (section 2) |
| Web page | http://localhost:8080 |
| RabbitMQ management UI | http://localhost:15672, log in with `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` from `.env` |
| Docker Hub | https://hub.docker.com/r/frametotoro/module_6 (tags `web-v1` and `worker-v1`, section 3) |
| Continuous integration | [`.github/workflows/module_6.yml`](../.github/workflows/module_6.yml): Pylint, Pytest, Docker build (section 8) |
| Pylint 10.00/10 | `cd module_6 && pylint src` (default settings, no `.pylintrc`) |
| Tests | 528 tests, 100% coverage of `module_6/src` (section 7) |
| Repository (SSH) | `git@github.com:sassom2112/jhu_software_concepts.git` (also in `github.txt`) |

Module 6 turns the Module 5 Grad Café application into a small microservice stack. The Flask page no
longer scrapes or recomputes anything inside a request. Its two buttons publish a task to RabbitMQ and
answer `202 Accepted` at once. A separate worker takes the tasks off the queue one at a time and does the
work against PostgreSQL, in one transaction per task. Docker Compose builds and starts all of it.

---

## 1. Architecture

```
 browser ──HTTP──> web (Flask, port 8080) ──publish──> RabbitMQ: exchange "tasks" (direct, durable)
                     │  SELECT only                        │  routing key "tasks"
                     │  (role gradcafe_web)                v
                     │                                   queue "tasks_q" (durable)
                     v                                     │  prefetch_count=1, manual acks
               db (PostgreSQL 17) <──── one transaction ── worker (python -m worker.consumer)
               named volume pgdata       per message        (role gradcafe_worker)
                     ^
                     └── init (one-shot, python -m worker.bootstrap, as the table owner):
                         tables + roles + applicant_data.json + first analysis snapshot
```

| Service | Image | What it does | Health check | Restart |
| --- | --- | --- | --- | --- |
| `db` | `postgres:17` | stores everything in the named volume `pgdata`; not published to the host | `pg_isready` over TCP | unless-stopped |
| `rabbitmq` | `rabbitmq:4.3-management` | the message broker and its management UI; queues and persistent messages live in the volume `rabbitmq_data` | `rabbitmq-diagnostics -q ping` | unless-stopped |
| `init` | `frametotoro/module_6:worker-v1` | runs `python -m worker.bootstrap` once, then exits 0 (section 4) | - | no |
| `web` | `frametotoro/module_6:web-v1` | the Flask page and its JSON endpoints, on `0.0.0.0:8080` inside the container | `GET /healthz` | unless-stopped |
| `worker` | `frametotoro/module_6:worker-v1` | consumes `tasks_q` and runs the two tasks (section 5) | - (RabbitMQ lists it as the queue's consumer) | unless-stopped |

All five share the project's default Compose network, which is a private bridge network. Only two ports
are published, both on `127.0.0.1`: the page and the RabbitMQ UI. PostgreSQL (5432) and AMQP (5672) can
be reached only inside that network. `web` and `worker` start after `init` has finished successfully and
RabbitMQ is healthy. The web, worker and init containers run as uid 1000, with a read-only root
filesystem, a writable `/tmp` only, no Linux capabilities and `no-new-privileges`.

**Message flow.** A click on a button sends `POST /pull-data` or `POST /update-analysis`.
`web/publisher.py` connects to `RABBITMQ_URL` and declares the durable exchange `tasks`, the durable queue
`tasks_q` and their binding (routing key `tasks`). It turns on publisher confirms and publishes one
compact, persistent message (`delivery_mode=2`):

```json
{"kind":"recompute_analytics","ts":"2026-10-05T18:00:00.123456+00:00","payload":{}}
```

The route answers `202` once RabbitMQ has confirmed the message, or `503` if the message could not be
queued. The worker receives the message, runs the task in one database transaction, commits, and only
then sends `basic_ack`. If anything fails, the transaction is rolled back and the message gets
`basic_nack(requeue=False)`. Meanwhile the page polls `GET /api/analysis-status` and reloads itself when
the stored analysis has a new `computed_at`.

## 2. Run the stack with Docker Compose

### What you need

* Docker Engine with the Compose v2 plugin (`docker compose version` must work), or Docker Desktop, which
  includes both. This was developed with Docker Engine 29.1 and Compose 2.40 on Ubuntu (WSL2). Check the
  install with `docker run hello-world`.
* git, and free ports 8080 and 15672 on `127.0.0.1` (or choose other ports in `.env`).
* Nothing else: Python, PostgreSQL and RabbitMQ all run in containers. The first build downloads the
  base images and the pinned Python packages, so it needs internet access.

### Start it

```bash
git clone git@github.com:sassom2112/jhu_software_concepts.git
cd jhu_software_concepts/module_6
cp .env.example .env
# Edit .env and replace every change-me.  Passwords go into connection URLs, so use only
# letters, digits, - and _ .  This prints a suitable one:
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
docker compose up --build
```

Run `docker compose` in `module_6/`, where `docker-compose.yml` and `.env` are. Without a `.env`, Compose
stops at once with a message such as `required variable RABBITMQ_DEFAULT_USER is missing a value: set
RABBITMQ_DEFAULT_USER in module_6/.env (see .env.example)`. The stack never starts with an empty password.

On the first start, `db` and `rabbitmq` become healthy. Then `init` creates the tables and the two
database roles, loads the 30,500 applicants from `src/data/applicant_data.json`, computes the first
analysis and exits with code 0. After that, `web` and `worker` start. All of this takes about a minute,
plus the image builds the first time. Add `-d` to run in the background, and then:

```bash
docker compose ps                 # db, rabbitmq, web: healthy; worker: running; init: exited (0)
docker compose logs -f worker     # each task: running ..., committed ..., acknowledged
docker compose down               # stop and remove the containers; the data stays in the volumes
docker compose down -v            # ... and delete the volumes too (the database and the queues)
```

The volumes keep the database between runs, so the next `docker compose up` starts with the same rows and
the same analysis. `init` runs again on every `up`. That is harmless: it inserts 0 rows and leaves a
current analysis alone. PostgreSQL and RabbitMQ store their passwords the first time they start. To change
`POSTGRES_PASSWORD` or the RabbitMQ user later, first run `docker compose down -v`, which deletes the data;
`init` then loads it again.

### Ports

| URL | What | Change it with |
| --- | --- | --- |
| http://localhost:8080 | the analysis page (`/` redirects to `/analysis`) | `WEB_PORT` |
| http://localhost:15672 | RabbitMQ management UI | `RABBITMQ_UI_PORT` |

**RabbitMQ login:** use `RABBITMQ_DEFAULT_USER` and `RABBITMQ_DEFAULT_PASS` from your `.env`. The
assignment mentions `guest`/`guest` for development. RabbitMQ, however, accepts `guest` only from inside
its own container, and a stack that anyone can clone should not ship a well-known password. When these two
variables are set, the RabbitMQ image does not create `guest` at all. In the UI, **Exchanges → tasks**
shows the durable direct exchange. **Queues and Streams → tasks_q** shows the durable queue, its binding,
its one consumer and that consumer's prefetch count of 1.

### Environment variables (`.env`)

`.env.example` lists every name with placeholder values. `.env` is git-ignored, so the real values are
never committed. Compose reads `module_6/.env` by itself.

| Variable | Used by | Meaning | Default |
| --- | --- | --- | --- |
| `POSTGRES_PASSWORD` | db, init | password of `gradcafe`, PostgreSQL's administrator and the owner of the tables; only `init` logs in as it | required |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | rabbitmq, web, worker | the broker account; Compose builds `RABBITMQ_URL=amqp://USER:PASSWORD@rabbitmq:5672/` from them | required |
| `WEB_DB_USER` / `WEB_DB_PASSWORD` | init, web | the web app's read-only database role | `gradcafe_web` / required |
| `WORKER_DB_USER` / `WORKER_DB_PASSWORD` | init, worker | the worker's database role | `gradcafe_worker` / required |
| `WEB_PORT` | web | host port of the page, on 127.0.0.1 | `8080` |
| `RABBITMQ_UI_PORT` | rabbitmq | host port of the management UI, on 127.0.0.1 | `15672` |
| `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD` | code run outside Docker | connection settings for the app on this machine (section 6) | unset |

Inside the containers, the services also get `DATABASE_URL` (each for its own role) and `RABBITMQ_URL`
from `docker-compose.yml`. The web image sets `FLASK_HOST=0.0.0.0` and `PORT=8080`. The worker image sets
`DATA_DIR=/app/data` and `SCRAPE_DATA_DIR=/tmp/gradcafe_scrape`. No URL or password is ever logged.

### The two buttons

| Button | Request | Task published | Answer |
| --- | --- | --- | --- |
| **Pull Data** | `POST /pull-data` | `scrape_new_data` | `202 {"ok": true, "queued": true, "kind": "scrape_new_data"}` |
| **Update Analysis** | `POST /update-analysis` | `recompute_analytics` | `202 {"ok": true, "queued": true, "kind": "recompute_analytics"}` |

Both answer at once. If RabbitMQ cannot be reached, or does not confirm the message, the answer is
`503 {"ok": false, "queued": false, "error": "the task could not be queued; try again in a minute"}`, and
nothing was queued. After a `202`, the page shows a "Request queued" banner (`role="status"`). It asks
`GET /api/analysis-status` every 3 seconds for up to 2 minutes and reloads when the analysis has a new
"Analysis computed at" time. A pull fetches only the entries newer than the newest one stored, so it
usually takes a few seconds. Extra clicks simply wait in the queue, because the worker runs one task at a
time.

From a shell, with the stack running:

```bash
curl -i -X POST http://localhost:8080/update-analysis        # HTTP/1.1 202 ACCEPTED
curl -s http://localhost:8080/api/analysis-status            # {"computed_at": "...", "ok": true, "total_entries": 30500}
```

Other endpoints:

* `GET /analysis` is the page.
* `GET /api/applicants` is the Module 5 search API, unchanged. It takes `term`, `status`, `degree`,
  `us_or_international`, `program`, `sort`, `order` and `limit` (clamped to 1-100).
* `GET /healthz` answers `ok` without touching the database. The health check uses it.

## 3. Docker images and Docker Hub

| Image | Dockerfile | Contents | Command |
| --- | --- | --- | --- |
| `frametotoro/module_6:web-v1` | `src/web/Dockerfile` | `db/` and `web/` only: no worker code, no SQLAlchemy, no scraper libraries | `python -m web.run` |
| `frametotoro/module_6:worker-v1` | `src/worker/Dockerfile` | `db/` and `worker/`; also used by the `init` service | `python -m worker.consumer` |

Both images start from `python:3.11-slim` and install `src/<service>/requirements.txt`. That file is a
complete list of exact `==` pins, with the same versions as `module_6/requirements.txt`. It is installed
with `--no-deps` and checked with `pip check`. Both images run as `USER 1000:1000`. The code stays owned
by root, so the running service cannot change it. The build context is `module_6/src`, so both images can
copy the shared `db/` package. `src/.dockerignore` keeps the 36 MB data file out of the images; the stack
mounts `src/data` read-only at `/app/data` instead.

`src/web/run.py` binds `FLASK_HOST:PORT`. Outside Docker, the host defaults to `127.0.0.1`, so a
development server stays private. The web image sets `FLASK_HOST=0.0.0.0` and `PORT=8080`. Inside the
container the page therefore listens on `0.0.0.0:8080`, and Docker can forward the port to it.

**Registry:** https://hub.docker.com/r/frametotoro/module_6 (a public repository named `module_6`).

Pull the images:

```bash
docker pull frametotoro/module_6:web-v1
docker pull frametotoro/module_6:worker-v1
```

To run the whole stack from the pulled images instead of building them, go to `module_6/`, create a
`.env` (section 2) and run `docker compose up --no-build`. Compose then uses the images named in
`docker-compose.yml`.

To check one container by itself:

```bash
docker run --rm -p 127.0.0.1:8080:8080 frametotoro/module_6:web-v1
# http://localhost:8080/healthz answers ok; the page says the database is not reachable
docker run --rm frametotoro/module_6:worker-v1
# exits with code 1: RABBITMQ_URL is not set (the worker needs the rest of the stack)
```

To build, tag and push (the repository owner, after `docker login`):

```bash
cd module_6
docker compose build              # builds and tags frametotoro/module_6:web-v1 and :worker-v1
docker push frametotoro/module_6:web-v1
docker push frametotoro/module_6:worker-v1
```

## 4. Database initialization and least privilege

The schema migration is **`python -m worker.bootstrap`** (`src/worker/bootstrap.py`). The `init` service
runs it on every `docker compose up`, as `gradcafe`, the owner of the tables. It does the following in one
transaction:

1. It creates the tables that are missing (`db/load_data.create_schema`):
   * `applicants`, Module 3's table;
   * `ingestion_watermarks`, exactly as the assignment gives it (`source TEXT PRIMARY KEY,
     last_seen TEXT, updated_at TIMESTAMPTZ DEFAULT now()`);
   * `analysis_snapshot`, one row that holds `computed_at` and the analysis as JSONB: the summary the
     page shows.
2. It creates or resets the two service roles (`db/db_roles.py`).
3. It loads `src/data/applicant_data.json` with `db/load_data.py`, using
   `INSERT ... ON CONFLICT (p_id) DO NOTHING`, so a second run inserts 0 rows.
4. It computes the first analysis snapshot if there is none yet or step 3 added rows.

Run it again by hand with `docker compose run --rm init`. Exit codes: 0 done, 1 unusable settings,
2 cannot connect, 3 a statement failed (nothing was changed).

| Role | May | May not |
| --- | --- | --- |
| `gradcafe_web` (web) | `SELECT` on `applicants` and `analysis_snapshot` | write anything, or read `ingestion_watermarks` |
| `gradcafe_worker` (worker) | `SELECT, INSERT` on `applicants`; `SELECT, INSERT, UPDATE` on `ingestion_watermarks` and `analysis_snapshot` | `UPDATE` or `DELETE` applicants, `TRUNCATE`, `DROP`, `ALTER`, `CREATE` |

Both roles are `NOSUPERUSER NOCREATEDB NOCREATEROLE`, with a connection limit and a statement timeout, and
neither owns a table. Their passwords come from `.env` and are never printed. As in Module 5, all SQL is
built with psycopg's `sql` module, with bound parameters, allow-listed identifiers and a `LIMIT` on every
`SELECT`. The web app reads in read-only transactions.

## 5. The worker

`src/worker/consumer.py` is a long-running process:

* It connects to `RABBITMQ_URL`. While the broker is still starting, or its host name does not resolve
  yet, it retries up to 10 times, 3 seconds apart, and logs one line per attempt.
* It declares the same durable exchange, queue and binding as the publisher (idempotent), sets
  `basic_qos(prefetch_count=1)` and consumes with manual acknowledgements.
* For each message, it parses the JSON and routes it by `kind` through the task map
  `TASKS = {"scrape_new_data": handle_scrape_new_data, "recompute_analytics": handle_recompute_analytics}`.
  It opens a database connection as the worker role and runs the handler inside
  `with conn.transaction():`. `basic_ack` is sent only after the commit.
* Every other outcome gets `basic_nack(requeue=False)`: the message is logged and dropped, never
  requeued, so a bad task cannot loop forever, and the worker keeps running. This covers a body over
  64 KiB, a body that is not JSON (or is nested too deeply to read), JSON that is not an object, an
  unknown kind, a bad payload, and any error in the handler (rolled back first).
* Each task runs in its own thread, so pika's thread keeps answering heartbeats during a long pull. The
  ack goes back to pika's thread through `add_callback_threadsafe`.
* If the broker connection is lost, the process exits with code 3 and Docker restarts it. RabbitMQ then
  delivers any unacknowledged task again, and the log marks it `(redelivered)`. This is harmless because
  both handlers are idempotent. The other exit codes are 0 (stopped by `docker stop`), 1 (no usable
  `RABBITMQ_URL`) and 2 (the broker stayed unreachable).

**`handle_scrape_new_data(conn, payload)`** first reads the watermark. That is
`ingestion_watermarks.last_seen` for the source `gradcafe`, or `payload["since"]` when it is given (a
whole number), or, before the first pull, the highest `p_id` already stored. It then runs the existing
scraper through `worker/etl/incremental_scraper.py`. The scraper checks `robots.txt`, starts at the newest
listing page and pauses between pages. It stops at the first page that reaches the watermark and never
reads more than 50 pages. The new entries are cleaned with `clean.py` and inserted with
`ON CONFLICT (p_id) DO NOTHING`. The watermark is set to the highest result id seen, and it never moves
backwards. Finally the analysis snapshot is recomputed, so the page shows the new rows. All of this
happens in the message's one transaction.

**`handle_recompute_analytics(conn, payload)`** recomputes all eleven analysis answers with SQL
(`worker/etl/query_data.py`) and stores them in `analysis_snapshot`, in the same per-message transaction.
The page reads only that row, so the web app never runs the analysis itself.

## 6. Local development (without Docker)

Use Python 3.11 (`.python-version` pins 3.11.13). From `module_6`:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # exact versions: the app, pika, the tests, Pylint, pydeps
pip install -e .                  # the project itself, editable (keep the -e)
pip check
```

`uv venv .venv && uv pip sync requirements.txt && uv pip install -e .` builds the same environment.

The tests need a PostgreSQL database whose name ends in `_test`; the fixtures refuse any other. One way to
get one is a throwaway container on localhost:

```bash
docker run -d --name gradcafe-test-db -e POSTGRES_USER=gradcafe -e POSTGRES_DB=gradcafe_test \
  -e POSTGRES_PASSWORD=choose-a-password -p 127.0.0.1:5432:5432 postgres:17
echo 'localhost:5432:*:gradcafe:choose-a-password' >> ~/.pgpass && chmod 600 ~/.pgpass
```

The commands `gradcafe-bootstrap` (`python -m worker.bootstrap`), `gradcafe-load` and `gradcafe-roles` also
work outside Docker. Give them the owner's `DATABASE_URL`, for example
`DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe gradcafe-bootstrap`. `python -m web.run`
serves the page on `127.0.0.1:8080` and logs in through `DB_*` (see `.env.example`). Its buttons need a
`RABBITMQ_URL`. `python -m worker.consumer` runs the worker.

## 7. Tests

Run the tests from the **repository root**, because `pytest.ini` measures `--cov=module_6/src`:

```bash
cd ..    # jhu_software_concepts/
DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe_test \
  module_6/.venv/bin/python -m pytest module_6 -m "web or buttons or analysis or db or integration"
```

Result: `528 passed` and `Required test coverage of 100% reached. Total coverage: 100.00%`, in about
10 seconds. No test needs RabbitMQ, Docker or the internet: pika's connection, the broker and Grad Café
are faked. The tests do use a real `*_test` database.

| File | Tests | What it proves |
| --- | --- | --- |
| `test_publisher.py` | 18 | durable exchange, queue and binding; publisher confirms; compact JSON with `kind`, UTC `ts` and `payload`; `delivery_mode=2`; headers; the connection is always closed; errors propagate and become 503; a publish to a blocked broker times out |
| `test_consumer.py` | 66 | ack only after the commit; rollback and `nack(requeue=False)` for a failing task; malformed, oversized and deeply nested bodies are nacked without a crash; the database connection is closed after each message; declarations, `prefetch_count=1` and manual acks; start-up retries, also for an unknown host; exit codes; redeliveries are logged |
| `test_watermark.py` | 28 | the watermark is read first; `payload["since"]`; only newer entries are fetched; the highest id is stored; a second pull inserts 0; robots.txt and the 50-page cap |
| `test_snapshot.py` | 13 | the snapshot round trip; the SQL snapshot equals the ORM answers; the page with and without a snapshot; `computed_at` is the time the snapshot was written |
| `test_bootstrap.py` | 14 | schema, roles, data and snapshot from nothing; a second run is harmless; each role's exact privileges |
| `test_containers.py` | 18 | both Dockerfiles (pins, uid 1000, `0.0.0.0:8080`, no worker code in the web image) and `docker-compose.yml` (services, named volume, read-only data, localhost ports, secrets from `.env`, each service's own role, hardening) |
| `test_web_service.py` | 9 | `/healthz`, `/api/analysis-status`, the queued banner and its polling script; the web code imports nothing from the worker |
| `test_buttons.py`, `test_flask_page.py`, `test_app_wiring.py` | 11, 5, 5 | both buttons answer 202 and queue the right task, or 503; the page, its selectors and the app factory |
| `test_integration_end_to_end.py`, `test_db_insert.py` | 3, 10 | click → the real publisher's bytes → the real worker → snapshot → page; what a pull writes |
| `test_db_hardening.py`, `test_sql_injection.py` | 57, 38 | settings and secrets; what both roles are refused; the Module 5 SQL injection attacks on `GET /api/applicants` |
| `test_query_tools.py`, `test_analysis_format.py`, `test_load_data.py`, `test_db_config.py`, `test_clean.py` | 19, 21, 31, 6, 51 | the analysis in SQL and through the ORM, formatting, the loader, connection settings, the cleaning rules |
| `test_scrape_parsing.py`, `test_scrape_http.py`, `test_scrape_full.py` | 39, 31, 35 | the scraper against the fake Grad Café (`tests/fake_gradcafe.py`) |

## 8. Pylint and GitHub Actions

```bash
cd module_6
pylint src                        # Your code has been rated at 10.00/10
```

Pylint uses its default settings; there is no `.pylintrc`. Five `pylint: disable` comments remain, and
each explains why the check is wrong where it is used:

* `not-callable` in `orm_queries.py`, because SQLAlchemy generates `func.count()` and similar at run time;
* `too-few-public-methods` on the ORM classes `Base` and `Applicant`;
* `invalid-name` on the session factory `SessionLocal`;
* `broad-exception-caught` on the worker's last-resort `except Exception`, which makes sure every
  delivery is answered.

GitHub runs only the workflows stored in `.github/workflows` at the repository root.
[`module_6.yml`](../.github/workflows/module_6.yml) runs on every push or pull request that changes
`module_6/`. It has three jobs, on Ubuntu 24.04 with Python 3.11:

| Job | What it runs | Fails when |
| --- | --- | --- |
| Pylint | `pylint src --fail-under=10` | the score is below 10.00/10 |
| Pytest | a `postgres:17` service with a `gradcafe_test` database, then `pytest module_6 -m "web or buttons or analysis or db or integration"` from the repository root | any test fails, or coverage is below 100% |
| Docker | `docker compose config` and `docker compose build` with the placeholder `.env.example`; both images must run as `1000:1000` | the compose file is invalid, or an image does not build or would run as another user |

The older workflows stay with their own folders. `ci.yml` (Module 5: Pylint, pydeps, Snyk, Pytest) runs
only for changes to `module_5/`, and `tests.yml` (Module 4) only for `module_4/`. They never run against
Module 6 or block it.

## 9. What is in this folder

```
module_6/
├── README.md                 # this file
├── docker-compose.yml        # db, rabbitmq, init, web, worker; volumes pgdata and rabbitmq_data
├── .env.example              # every setting, placeholder values only (copy to .env)
├── setup.py, requirements.txt, pytest.ini, .python-version
├── src/                      # the application, and the build context of both images
│   ├── web/                  # Dockerfile, requirements.txt, run.py, publisher.py,
│   │                         # app/ (create_app, routes, services, applicant_search, templates)
│   ├── worker/               # Dockerfile, requirements.txt, consumer.py, bootstrap.py (DB init),
│   │   └── etl/              # incremental_scraper.py, scrape.py (+ helpers), clean.py, ingest.py,
│   │                         # query_data.py, analytics.py, orm_queries.py, models.py, ...
│   ├── db/                   # shared by both images: load_data.py (JSON -> SQL loader, schema,
│   │                         # watermark), snapshot.py, db_roles.py, db_config.py, query_limits.py
│   └── data/applicant_data.json   # the 30,500 LLM-cleaned applicants (mounted read-only)
├── tests/                    # the 528 tests (section 7)
├── docs/                     # Sphinx documentation carried over from Module 4
└── data/                     # the command-line scraper's folder (raw_entries.json.gz, robots.txt)
```

`module_6/data/` belongs to the command-line scraper (`python -m worker.etl.scrape`), which reads and
writes its files there, as in Modules 2-5. The stack does not use it: the worker scrapes into
`SCRAPE_DATA_DIR` inside its container, and `init` loads `src/data/applicant_data.json`. The Sphinx
sources in `docs/` still describe the Module 4 layout. Updating them is optional for this module, so this
README and the docstrings in `src/` are the Module 6 documentation. The `.gitignore` rules and the
workflows live at the repository root.

## 10. Known limitations

* A pull reads at most 50 listing pages, about 1,000 entries. If more new entries than that ever pile up,
  the watermark still moves to the newest id, so the older part of the gap is skipped. The worker logs a
  warning when this happens. Between pulls Grad Café usually adds far fewer entries; the live pull read
  2 pages.
* Rows added by a pull have empty LLM columns. Question 11 and the LLM-field count of Question 9 do not
  include them until the optional `llm_hosting` standardizer of the earlier modules is run.
* Web, worker and the management UI share one RabbitMQ account, the one from `.env`. Separate broker
  users for publishing and consuming would need a RabbitMQ definitions file.
* The stack runs one worker. The queue and `prefetch_count=1` serialize the tasks; there is no database
  lock, so `--scale worker=2` is not supported.
* The data are self-reported Grad Café submissions (see `module_3/limitations.pdf`).
