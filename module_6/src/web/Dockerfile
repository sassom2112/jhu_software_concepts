# The web service: the Flask analysis page and its two buttons, which only queue tasks.
#
# Build context = module_6/src (docker-compose.yml sets it), so the image can also take
# the shared db/ package:
#   docker build -f web/Dockerfile -t frametotoro/module_6:web-v1 .     (run in module_6/src)
# It holds db/ and web/ only: no worker code, no SQLAlchemy, no scraper libraries.
FROM python:3.11-slim

WORKDIR /app

# Dependencies first, so this layer is rebuilt only when requirements.txt changes.
# The list is the complete, exactly pinned closure: --no-deps installs exactly it, and
# pip check fails the build if anything were missing.
COPY web/requirements.txt .
RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check

COPY db/ db/
COPY web/ web/

# A user of its own (uid/gid 1000) with no login shell and no home directory; the code
# stays owned by root, so the running app cannot change it.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app

# web.run binds FLASK_HOST:PORT; 0.0.0.0 so Docker can forward port 8080 to it.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080
USER 1000:1000
CMD ["python", "-m", "web.run"]
