"""db - Database access shared by the web and worker images: settings, schema, loader and roles.

The schema migration that uses this package is ``python -m worker.bootstrap``
(src/worker/bootstrap.py), which docker-compose.yml runs as the one-shot init
service: tables, both service roles, the applicant data and the first snapshot.
"""
