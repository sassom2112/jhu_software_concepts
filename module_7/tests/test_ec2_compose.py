"""
Static checks of ec2/docker-compose.ec2.yml and ec2/.env.example.

These read the YAML only; they do not start Docker.  They pin down the
deployment rules from the assignment, so an edit that would publish
PostgreSQL, open the RabbitMQ UI to the internet or build on the server fails
here first.  (`docker compose ... config -q` and a real `up` are the other
checks; EC2_DEPLOYMENT.md shows both.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

EC2_DIR = Path(__file__).resolve().parents[1] / "ec2"
COMPOSE_FILE = EC2_DIR / "docker-compose.ec2.yml"
ENV_EXAMPLE = EC2_DIR / ".env.example"
VARIABLE = re.compile(r"\$\{(\w+)(:[-?][^}]*)?\}")


@pytest.fixture(name="compose", scope="module")
def fixture_compose() -> dict:
    """The compose file, parsed (YAML anchors and << merges resolved)."""
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _compose_code() -> str:
    """The compose file without its comment lines (the header quotes ${NAME:?...})."""
    lines = COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _env_example() -> dict[str, str]:
    """NAME=value lines of .env.example."""
    pairs = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            pairs[name.strip()] = value.strip()
    return pairs


def test_the_four_required_services_and_init(compose):
    """Flask web, worker, PostgreSQL and RabbitMQ, plus the one-shot init."""
    assert set(compose["services"]) == {"db", "rabbitmq", "init", "web", "worker"}


def test_images_only_nothing_is_built_on_the_server(compose):
    """Published images: the student's Docker Hub images and the versions the assignment names."""
    services = compose["services"]
    assert all("build" not in service for service in services.values())
    assert services["db"]["image"] == "postgres:16"
    assert services["rabbitmq"]["image"] == "rabbitmq:3.13-management"
    assert services["web"]["image"] == "frametotoro/module_6:web-v1"
    assert services["worker"]["image"] == "frametotoro/module_6:worker-v1"
    assert services["init"]["image"] == "frametotoro/module_6:worker-v1"


def test_only_the_page_is_public_and_the_rabbitmq_ui_is_loopback_only(compose):
    """5432 and 5672 are never published; 15672 only on 127.0.0.1; the page on 8080."""
    services = compose["services"]
    published = {name: service.get("ports", []) for name, service in services.items()}
    assert published["db"] == [] and published["init"] == [] and published["worker"] == []
    assert published["web"] == ["${WEB_PORT:-8080}:8080"]
    assert published["rabbitmq"] == ["127.0.0.1:${RABBITMQ_UI_PORT:-15672}:15672"]


def test_postgres_keeps_its_data_in_a_named_volume(compose):
    """Persistent storage: pgdata is a named volume, not a folder on the server."""
    assert "pgdata:/var/lib/postgresql/data" in compose["services"]["db"]["volumes"]
    assert "pgdata" in compose["volumes"]


def test_only_init_mounts_the_data_file_read_only(compose):
    """./data/applicant_data.json, read-only, and a missing file is an error."""
    services = compose["services"]
    (mount,) = services["init"]["volumes"]
    assert mount["type"] == "bind" and mount["read_only"] is True
    assert mount["source"] == "./data/applicant_data.json"
    assert mount["target"] == "/app/data/applicant_data.json"
    assert mount["bind"]["create_host_path"] is False   # a missing file is an error
    assert "volumes" not in services["web"] and "volumes" not in services["worker"]


def test_start_order_waits_for_health_and_for_init(compose):
    """db and rabbitmq healthy, init finished, then web and worker."""
    services = compose["services"]
    assert services["init"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    for name in ("web", "worker"):
        assert services[name]["depends_on"] == {
            "db": {"condition": "service_healthy"},
            "rabbitmq": {"condition": "service_healthy"},
            "init": {"condition": "service_completed_successfully"},
        }
    assert all("healthcheck" in services[name] for name in ("db", "rabbitmq", "web"))


def test_every_secret_is_required_from_env():
    """Passwords and the RabbitMQ user use ${NAME:?message}: no silent empty password."""
    uses = VARIABLE.findall(_compose_code())
    assert uses
    for name, modifier in uses:
        if "PASS" in name or name == "RABBITMQ_DEFAULT_USER":
            assert modifier.startswith(":?"), name


def test_env_example_lists_exactly_the_variables_the_file_uses():
    """Every ${NAME} has a line in .env.example, and every password there is a placeholder."""
    used = {name for name, _modifier in VARIABLE.findall(_compose_code())}
    example = _env_example()
    assert used == set(example)
    for name, value in example.items():
        if "PASS" in name:
            assert value == "change-me", name
