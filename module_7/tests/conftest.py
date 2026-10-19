"""
Shared fixtures: a fake AWS account (moto) so that no test ever talks to real AWS.

aws_env  - clears every AWS setting of the machine running the tests (profiles,
           ~/.aws files, endpoints, the instance metadata service) and sets
           obviously fake keys, so boto3 can only use these.
s3       - inside moto's mock_aws(): the bucket s3_fetch.DEFAULT_BUCKET holding a
           small applicant_data.json (SAMPLE_BODY); yields a boto3 S3 client for it.
sample_body - the bytes of that applicant_data.json.  Tests take it as a fixture
           instead of importing conftest, which pylint run on the folder cannot resolve.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from s3_fetch import DEFAULT_BUCKET

REGION = "us-east-1"
FAKE_KEY_ID = "fake-key-id-for-tests"
FAKE_SECRET = "fake-secret-for-tests"
SAMPLE_RECORDS = [
    {"result_id": 3, "program": "Physics", "status": "Accepted"},
    {"result_id": 2, "program": "Chemistry", "status": "Rejected"},
    {"result_id": 1, "program": "Biology", "status": "Accepted"},
]
SAMPLE_BODY = json.dumps(SAMPLE_RECORDS, indent=2).encode("utf-8")

_CLEARED = (
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "GRAD_CAFE_BUCKET",
)


@pytest.fixture(name="aws_env")
def fixture_aws_env(monkeypatch, tmp_path):
    """Fake keys in the environment and nothing else; returns the region."""
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials-file"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config-file"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", FAKE_SECRET)
    return REGION


@pytest.fixture(name="sample_body")
def fixture_sample_body():
    """The bytes of the fake applicant_data.json that the s3 fixture puts in the bucket."""
    return SAMPLE_BODY


@pytest.fixture(name="s3")
def fixture_s3(aws_env):
    """A moto S3 with s3://<DEFAULT_BUCKET>/applicant_data.json; yields the client."""
    with mock_aws():
        client = boto3.client("s3", region_name=aws_env)
        client.create_bucket(Bucket=DEFAULT_BUCKET)
        client.put_object(Bucket=DEFAULT_BUCKET, Key="applicant_data.json", Body=SAMPLE_BODY)
        yield client
