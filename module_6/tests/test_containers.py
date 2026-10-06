"""
test_containers.py - The Docker files, checked as text (no Docker, no network needed).

The stack itself is verified by running it (docker compose up --build, see the
README).  These tests guard the promises that are easy to break by editing a
file and hard to notice:

  * each image installs an exactly pinned list, with the same versions as
    module_6/requirements.txt, and every third-party package its code
    imports is on its list (the web list has no SQLAlchemy and no scraper);
  * both Dockerfiles run the service as uid 1000, not root, and the web image
    takes no worker code and listens on 0.0.0.0:8080;
  * docker-compose.yml defines the five services, keeps PostgreSQL's data in a
    named volume, mounts the data read-only, publishes only on 127.0.0.1,
    and takes every secret from module_6/.env, never from the file itself;
  * web and worker log in as their own least-privilege roles (only init uses
    the table owner), and every container of our images runs read-only,
    without capabilities and with no-new-privileges.
"""

from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MODULE = Path(__file__).resolve().parent.parent      # module_6/
SRC = MODULE / "src"
FIRST_PARTY = {"db", "web", "worker"}
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9.]*)$")


def normalized(name: str) -> str:
    """A distribution name the way pip compares them: Flask == flask, typing_extensions == typing-extensions."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path) -> dict[str, str]:
    """{distribution: version} of a requirements file; every line must be an exact pin."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            match = PIN.match(line)
            assert match, f"{path.name}: {line!r} is not an exact name==version pin"
            found[normalized(match.group(1))] = match.group(2)
    return found


def imported_distributions(*packages: str) -> set[str]:
    """The installed distributions that the code of *packages* (folders of src/) imports."""
    modules = set()
    for package in packages:
        for path in (SRC / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules.add(node.module.split(".")[0])
    third_party = modules - FIRST_PARTY - set(sys.stdlib_module_names)
    owners = packages_distributions()
    return {normalized(owners[module][0]) for module in third_party}


DEV = pins(MODULE / "requirements.txt")
WEB = pins(SRC / "web" / "requirements.txt")
WORKER = pins(SRC / "worker" / "requirements.txt")


# --------------------------------------------------------------------------- #
#                     requirements.txt of each service                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("service, pinned", [("web", WEB), ("worker", WORKER)])
def test_each_image_pins_the_versions_the_tests_ran_with(service, pinned):
    assert pinned, f"{service}/requirements.txt is empty"
    assert {name: DEV.get(name) for name in pinned} == pinned


@pytest.mark.parametrize("service, pinned", [("web", WEB), ("worker", WORKER)])
def test_every_package_the_code_imports_is_pinned_for_its_image(service, pinned):
    needed = imported_distributions("db", service)

    assert needed - set(pinned) == set()
    assert "pika" in needed                               # both talk to RabbitMQ


def test_the_web_image_has_no_sqlalchemy_and_no_scraper_libraries():
    assert {"sqlalchemy", "greenlet", "beautifulsoup4", "soupsieve", "lxml"} & set(WEB) == set()
    assert "flask" not in WORKER


# --------------------------------------------------------------------------- #
#                                Dockerfiles                                   #
# --------------------------------------------------------------------------- #

def dockerfile(service: str) -> list[str]:
    """The instructions of src/<service>/Dockerfile, comments and blank lines removed."""
    text = (SRC / service / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


@pytest.mark.parametrize("service", ["web", "worker"])
def test_both_images_start_from_python_311_and_run_as_uid_1000(service):
    lines = dockerfile(service)

    assert lines[0] == "FROM python:3.11-slim"
    assert f"COPY {service}/requirements.txt ." in lines
    assert "RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check" in lines
    users = [line for line in lines if line.startswith("USER ")]
    assert users == ["USER 1000:1000"]                    # set once, and never back to root
    assert lines.index(users[0]) > max(i for i, line in enumerate(lines) if line.startswith("RUN"))


def test_the_web_image_serves_on_0_0_0_0_port_8080_without_worker_code():
    lines = dockerfile("web")

    assert "EXPOSE 8080" in lines
    assert any("FLASK_HOST=0.0.0.0" in line and "PORT=8080" in line for line in lines)
    assert lines[-1] == 'CMD ["python", "-m", "web.run"]'
    assert [line for line in lines if line.startswith("COPY")] == [
        "COPY web/requirements.txt .", "COPY db/ db/", "COPY web/ web/"]


def test_the_worker_image_runs_the_consumer():
    lines = dockerfile("worker")

    assert lines[-1] == 'CMD ["python", "-m", "worker.consumer"]'
    assert [line for line in lines if line.startswith("COPY")] == [
        "COPY worker/requirements.txt .", "COPY db/ db/", "COPY worker/ worker/"]


def test_the_build_context_leaves_the_data_file_out():
    ignored = (SRC / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "data/" in ignored                             # mounted read-only at run time instead


# --------------------------------------------------------------------------- #
#                             docker-compose.yml                               #
# --------------------------------------------------------------------------- #

COMPOSE = (MODULE / "docker-compose.yml").read_text(encoding="utf-8")


def service_block(name: str) -> str:
    """The text of one service, from its name to the next service (or the volumes section)."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w+:\n|^\S)", COMPOSE, re.M | re.S)
    assert match, f"no service {name} in docker-compose.yml"
    return match.group(1)


def test_compose_defines_the_four_required_services_and_the_init_job():
    services = COMPOSE.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]

    assert re.findall(r"^  (\w+):$", services, re.M) == ["db", "rabbitmq", "init", "web", "worker"]
    assert "image: postgres:17" in service_block("db")
    assert re.search(r"image: rabbitmq:[\d.]+-management", service_block("rabbitmq"))


def test_postgres_keeps_its_data_in_a_named_volume_and_is_not_published():
    db = service_block("db")

    assert "- pgdata:/var/lib/postgresql/data" in db
    assert re.search(r"^volumes:\n  pgdata:", COMPOSE, re.M)
    assert "ports:" not in db
    assert "pg_isready" in db                             # health check


def test_the_data_folder_is_mounted_read_only():
    for service in ("init", "worker"):
        assert "- ./src/data:/app/data:ro" in service_block(service)


def test_published_ports_listen_on_localhost_only():
    published = re.findall(r'ports:\n\s+- "([^"]+)"', COMPOSE)

    assert published == ["127.0.0.1:${RABBITMQ_UI_PORT:-15672}:15672", "127.0.0.1:${WEB_PORT:-8080}:8080"]


def test_the_services_wait_for_their_dependencies():
    for service in ("web", "worker"):
        block = service_block(service)
        assert re.search(r"init:\n\s+condition: service_completed_successfully", block)
        assert re.search(r"rabbitmq:\n\s+condition: service_healthy", block)
    assert re.search(r"db:\n\s+condition: service_healthy", service_block("init"))
    assert "healthcheck:" in service_block("web") and "/healthz" in service_block("web")


def test_every_secret_comes_from_the_env_file_and_none_is_written_here():
    variables = set(re.findall(r"\$\{(\w+)", COMPOSE))
    required = set(re.findall(r"\$\{(\w+):\?", COMPOSE))
    example = (MODULE / ".env.example").read_text(encoding="utf-8")

    assert {"POSTGRES_PASSWORD", "RABBITMQ_DEFAULT_PASS", "WEB_DB_PASSWORD",
            "WORKER_DB_PASSWORD"} <= required             # no password ever defaults to anything
    assert {name for name in variables if "PASS" in name} <= required
    for name in variables:
        assert f"export {name}=" in example               # every setting is documented
    assert not re.search(r"(PASSWORD|_PASS): +[^$\s]", COMPOSE)   # no literal password values


def test_web_and_worker_log_in_as_their_own_least_privilege_roles():
    web, worker, init = service_block("web"), service_block("worker"), service_block("init")

    assert "DATABASE_URL: postgresql://${WEB_DB_USER:-gradcafe_web}:${WEB_DB_PASSWORD:?" in web
    assert ("DATABASE_URL: postgresql://${WORKER_DB_USER:-gradcafe_worker}:${WORKER_DB_PASSWORD:?"
            in worker)
    assert "DATABASE_URL: postgresql://gradcafe:${POSTGRES_PASSWORD:?" in init   # the owner...
    for block in (web, worker):
        assert "POSTGRES_PASSWORD" not in block           # ...is never what a service logs in as
        assert "postgresql://gradcafe:" not in block


def hardening_anchor() -> str:
    """The text of the x-hardened anchor that the long-running containers merge in."""
    match = re.search(r"^x-hardened: &hardened\n(.*?)(?=^\S)", COMPOSE, re.M | re.S)
    assert match, "no x-hardened anchor in docker-compose.yml"
    return match.group(1)


def test_the_containers_are_hardened():
    anchor = hardening_anchor()

    assert re.search(r"^  read_only: true\b", anchor, re.M)          # code cannot be changed...
    assert re.search(r"^  tmpfs:\n    - /tmp\b", anchor, re.M)       # ...only /tmp is writable
    assert re.search(r"^  cap_drop: \[ALL\]", anchor, re.M)          # no Linux capabilities
    assert '- "no-new-privileges:true"' in anchor
    assert "<<: *hardened" in service_block("web")
    for service in ("init", "worker"):
        assert "<<: [*worker-image, *hardened]" in service_block(service)
