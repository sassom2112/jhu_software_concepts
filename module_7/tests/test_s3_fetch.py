"""
Offline tests of s3_fetch.py.  S3 is moto's fake (see conftest.py); a few tests
hand download_object a small fake client instead, for failures moto cannot
produce (a short download, no network).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from boto3.exceptions import RetriesExceededError
from botocore.exceptions import ClientError, CredentialRetrievalError, EndpointConnectionError

import s3_fetch
from s3_fetch import (DEFAULT_BUCKET, DEFAULT_OUTPUT, S3FetchError, credential_source,
                      default_bucket, download_object, explain_client_error,
                      fetch_applicant_data, main, make_session)

MODULE_DIR = Path(__file__).resolve().parents[1]


def _leftovers(folder: Path) -> list[str]:
    """Temporary .part files left in *folder* (there should never be any)."""
    return [path.name for path in folder.glob("*.part")]


class FakeClient:
    """Just enough of a boto3 S3 client for download_object: head_object reports
    *size*; download_file writes *body*, or raises *error* if one is given."""

    def __init__(self, size: int, body: bytes = b"[]", error: Exception | None = None):
        self.size, self.body, self.error = size, body, error

    def head_object(self, **_kwargs):
        """What S3's HEAD request returns (only the part download_object reads)."""
        if self.error:
            raise self.error
        return {"ContentLength": self.size}

    def download_file(self, _bucket: str, _key: str, filename: str) -> None:
        """Write the body where boto3 would."""
        Path(filename).write_bytes(self.body)


class DroppedConnectionClient(FakeClient):
    """head_object works, then the transfer gives up, as boto3 does after repeated
    time-outs in the middle of a download."""

    def download_file(self, _bucket: str, _key: str, filename: str) -> None:
        """Write part of the file, then fail the way boto3's transfer manager does."""
        Path(filename).write_bytes(b"[")
        raise RetriesExceededError(TimeoutError("read timed out"))


# --- the normal download ---------------------------------------------------------------

@pytest.mark.usefixtures("s3")
def test_fetch_saves_an_exact_copy(tmp_path, sample_body):
    """The object arrives byte for byte; the result describes it."""
    output = tmp_path / "applicant_data_SM.json"
    result = fetch_applicant_data(output=output)
    assert output.read_bytes() == sample_body
    assert (result.bucket, result.key) == (DEFAULT_BUCKET, "applicant_data.json")
    assert result.path == output
    assert result.size_bytes == len(sample_body)
    assert result.records == len(json.loads(sample_body))
    assert result.credential_source == "env"
    assert not _leftovers(tmp_path)


@pytest.mark.usefixtures("s3")
def test_default_output_is_applicant_data_sm_in_the_current_folder(tmp_path, monkeypatch,
                                                                   sample_body):
    """With no arguments the file lands in the current folder (the notebook's folder)."""
    monkeypatch.chdir(tmp_path)
    result = fetch_applicant_data()
    assert DEFAULT_OUTPUT == "applicant_data_SM.json"
    assert result.path == tmp_path.resolve() / DEFAULT_OUTPUT
    assert result.path.read_bytes() == sample_body


@pytest.mark.usefixtures("s3")
def test_describe_reports_the_download_but_no_secret(tmp_path, sample_body):
    """The printed report names the source, file, size, records and credential source."""
    text = fetch_applicant_data(output=tmp_path / "out.json").describe()
    assert f"s3://{DEFAULT_BUCKET}/applicant_data.json" in text
    assert f"{len(sample_body):,} bytes" in text
    assert "Records    3" in text
    assert "default chain: env" in text
    assert os.environ["AWS_ACCESS_KEY_ID"] not in text
    assert os.environ["AWS_SECRET_ACCESS_KEY"] not in text


@pytest.mark.usefixtures("s3")
def test_a_newer_download_replaces_an_older_file(tmp_path, sample_body):
    """Running the notebook again overwrites applicant_data_SM.json with the new copy."""
    output = tmp_path / "applicant_data_SM.json"
    output.write_text("old", encoding="utf-8")
    fetch_applicant_data(output=output)
    assert output.read_bytes() == sample_body


# --- choosing the bucket ----------------------------------------------------------------

@pytest.mark.usefixtures("aws_env")
def test_default_bucket_without_the_variable(monkeypatch):
    """No GRAD_CAFE_BUCKET (or a blank one) means DEFAULT_BUCKET."""
    assert default_bucket() == DEFAULT_BUCKET
    monkeypatch.setenv("GRAD_CAFE_BUCKET", "   ")
    assert default_bucket() == DEFAULT_BUCKET


def test_the_bucket_can_come_from_the_environment(s3, tmp_path, monkeypatch):
    """GRAD_CAFE_BUCKET=another-grad-cafe-bucket reads that bucket without editing any code."""
    s3.create_bucket(Bucket="another-grad-cafe-bucket")
    s3.put_object(Bucket="another-grad-cafe-bucket", Key="applicant_data.json", Body=b"[{}]")
    monkeypatch.setenv("GRAD_CAFE_BUCKET", "another-grad-cafe-bucket")
    result = fetch_applicant_data(output=tmp_path / "out.json")
    assert (result.bucket, result.records) == ("another-grad-cafe-bucket", 1)


# --- failures leave nothing half-written ---------------------------------------------

@pytest.mark.usefixtures("s3")
def test_a_missing_object_is_explained(tmp_path):
    """A wrong key: S3FetchError, and no file is written."""
    output = tmp_path / "out.json"
    with pytest.raises(S3FetchError, match="does not exist"):
        fetch_applicant_data(key="no-such-file.json", output=output)
    assert not output.exists()
    assert not _leftovers(tmp_path)


@pytest.mark.usefixtures("s3")
def test_a_missing_bucket_is_explained(tmp_path):
    """A wrong bucket name: S3FetchError naming the bucket."""
    with pytest.raises(S3FetchError, match="s3://no-such-bucket-here/"):
        fetch_applicant_data("no-such-bucket-here", output=tmp_path / "out.json")


@pytest.mark.parametrize("body, message", [
    (b"this is not json", "not valid JSON"),
    (b"\xff\xfe\x00", "not valid JSON"),
    (b'{"result_id": 1}', "not a list of applicant records"),
    (b"[1, 2, 3]", "not a list of applicant records"),
    (b"[]", "holds no applicant records"),
])
def test_a_file_that_is_not_the_applicant_list_is_rejected(s3, tmp_path, body, message):
    """Bad content never replaces a good applicant_data_SM.json."""
    s3.put_object(Bucket=DEFAULT_BUCKET, Key="applicant_data.json", Body=body)
    output = tmp_path / "applicant_data_SM.json"
    output.write_text("the previous good copy", encoding="utf-8")
    with pytest.raises(S3FetchError, match=message):
        fetch_applicant_data(output=output)
    assert output.read_text(encoding="utf-8") == "the previous good copy"
    assert not _leftovers(tmp_path)


def test_a_short_download_is_rejected(tmp_path):
    """The file on disk must have the size S3 reported."""
    output = tmp_path / "out.json"
    with pytest.raises(S3FetchError, match="downloaded 2 bytes, but the object has 10"):
        download_object(FakeClient(size=10), "grad-cafe", "applicant_data.json", output)
    assert not output.exists()
    assert not _leftovers(tmp_path)


def test_no_network_is_reported(tmp_path):
    """botocore's connection errors become S3FetchError."""
    error = EndpointConnectionError(endpoint_url="https://grad-cafe.s3.amazonaws.com/")
    with pytest.raises(S3FetchError, match="Could not connect"):
        download_object(FakeClient(size=2, error=error), "grad-cafe", "applicant_data.json",
                        tmp_path / "out.json")


def test_a_transfer_that_gives_up_is_reported(tmp_path):
    """boto3's RetriesExceededError (a connection that keeps dropping) becomes S3FetchError."""
    with pytest.raises(S3FetchError, match="Max Retries Exceeded"):
        download_object(DroppedConnectionClient(size=2), "grad-cafe", "applicant_data.json",
                        tmp_path / "out.json")
    assert not _leftovers(tmp_path)


def test_an_unwritable_destination_is_reported(tmp_path):
    """A folder that does not exist: S3FetchError, not a traceback."""
    with pytest.raises(S3FetchError, match="could not save"):
        download_object(FakeClient(size=2), "grad-cafe", "applicant_data.json",
                        tmp_path / "missing-folder" / "out.json")


@pytest.mark.parametrize("code, hint", [
    ("404", "does not exist"),
    ("NoSuchKey", "object does not exist"),
    ("NoSuchBucket", "bucket does not exist"),
    ("403", "access denied"),
    ("AccessDenied", "access denied"),
    ("ExpiredToken", "expired"),
    ("SlowDown", "S3 refused the request"),
])
def test_s3_error_codes_are_explained(code, hint):
    """Each error code gets a hint, and the code itself is kept in the message."""
    err = ClientError({"Error": {"Code": code, "Message": "x"}}, "HeadObject")
    text = explain_client_error(err, "grad-cafe", "applicant_data.json")
    assert text.startswith("s3://grad-cafe/applicant_data.json: ")
    assert hint in text and f"(S3 error code {code})" in text


# --- credentials: the default chain only ------------------------------------------------

@pytest.mark.usefixtures("aws_env")
def test_no_credentials_at_all_is_explained(monkeypatch):
    """Without any credential source the error says what to set up."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    with pytest.raises(S3FetchError, match="no AWS credentials found"):
        credential_source(make_session())


@pytest.mark.usefixtures("s3")
def test_a_named_profile_from_the_shared_files_is_used(tmp_path, monkeypatch):
    """--profile (or AWS_PROFILE) reads ~/.aws/credentials, as on a laptop."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    credentials = tmp_path / "credentials"
    credentials.write_text("[dailyWork-ms]\naws_access_key_id = profile-key-for-tests\n"
                           "aws_secret_access_key = profile-secret-for-tests\n", encoding="utf-8")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    result = fetch_applicant_data(output=tmp_path / "out.json",
                                  session=make_session("dailyWork-ms"))
    assert result.credential_source == "shared-credentials-file"


@pytest.mark.usefixtures("aws_env")
def test_an_unknown_profile_is_explained():
    """A profile name that is not in ~/.aws: S3FetchError naming it."""
    with pytest.raises(S3FetchError, match="'nobody' was not found"):
        make_session("nobody")


def test_a_credential_provider_failure_is_explained():
    """A provider that fails (e.g. a broken role setup) becomes S3FetchError."""

    def fail():
        raise CredentialRetrievalError(provider="assume-role", error_msg="no source")

    with pytest.raises(S3FetchError, match="could not load AWS credentials"):
        credential_source(SimpleNamespace(get_credentials=fail))


# --- the command line ------------------------------------------------------------------

@pytest.mark.usefixtures("s3")
def test_cli_downloads_and_prints_the_report(tmp_path, capsys, sample_body):
    """python s3_fetch.py --output ... prints the report and returns 0."""
    output = tmp_path / "cli.json"
    assert main(["--output", str(output)]) == 0
    assert output.read_bytes() == sample_body
    assert "Records    3" in capsys.readouterr().out


@pytest.mark.usefixtures("s3")
def test_cli_failure_returns_1_with_a_message(tmp_path, capsys):
    """A failed download prints "error: ..." on stderr and returns 1."""
    assert main(["--bucket", "no-such-bucket-here", "--output", str(tmp_path / "x.json")]) == 1
    assert capsys.readouterr().err.startswith("error: s3://no-such-bucket-here/")


@pytest.mark.usefixtures("aws_env")
def test_cli_unknown_profile_returns_1(capsys):
    """--profile with an unknown name fails cleanly too."""
    assert main(["--profile", "nobody"]) == 1
    assert "'nobody' was not found" in capsys.readouterr().err


def test_the_script_runs_from_its_folder():
    """python s3_fetch.py --help works as a script, from module_7/."""
    done = subprocess.run([sys.executable, "s3_fetch.py", "--help"], cwd=MODULE_DIR,
                          capture_output=True, text=True, check=False, timeout=60)
    assert done.returncode == 0
    unwrapped = "".join(done.stdout.split())   # argparse wraps long names at their hyphens
    assert "applicant_data_SM.json" in unwrapped
    assert s3_fetch.DEFAULT_BUCKET in unwrapped
