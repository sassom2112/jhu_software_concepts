# The worker service: consumes the tasks the web app queues (python -m worker.consumer).
# docker-compose.yml also runs this image once as the "init" service, with the command
# python -m worker.bootstrap, to create the tables and roles and load the data.
#
# Build context = module_6/src (docker-compose.yml sets it), so the image can also take
# the shared db/ package:
#   docker build -f worker/Dockerfile -t frametotoro/module_6:worker-v1 .  (run in module_6/src)
# It holds db/ and worker/ only.  The applicant data is not copied in: the stack mounts
# src/data read-only at /app/data.
FROM python:3.11-slim

WORKDIR /app

# Dependencies first, so this layer is rebuilt only when requirements.txt changes.
# The list is the complete, exactly pinned closure: --no-deps installs exactly it, and
# pip check fails the build if anything were missing.
COPY worker/requirements.txt .
RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check

COPY db/ db/
COPY worker/ worker/

# A user of its own (uid/gid 1000) with no login shell and no home directory; the code
# stays owned by root, so the running worker cannot change it.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app

# DATA_DIR: where python -m worker.bootstrap reads applicant_data.json.  SCRAPE_DATA_DIR:
# the scraper's files go to /tmp, the one place this user may write.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    SCRAPE_DATA_DIR=/tmp/gradcafe_scrape

USER 1000:1000
CMD ["python", "-m", "worker.consumer"]
