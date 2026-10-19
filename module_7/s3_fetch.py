"""
s3_fetch.py - Download the Grad Café data from Amazon S3 with boto3.

The notebook grad-cafe-pipeline.ipynb imports this module.  It also runs on
its own, from this folder::

    python s3_fetch.py                          # s3://<DEFAULT_BUCKET>/applicant_data.json
    python s3_fetch.py --bucket another-bucket  # another bucket
    python s3_fetch.py --profile dailywork      # a named profile from ~/.aws
    python s3_fetch.py --help

Either way it downloads one object (applicant_data.json by default) and saves
it in the current folder as applicant_data_SM.json.

Credentials: this file holds none and never asks for any.  boto3.Session()
finds them with boto3's default credential chain, which tries, in order:
environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY), the shared
files ~/.aws/credentials and ~/.aws/config (AWS_PROFILE or --profile picks a
profile), and finally the IAM role of the machine it runs on.  On a SageMaker
notebook instance the chain finds the notebook's execution role, so the
notebook needs no access keys at all.  The result reports which source was
used, as botocore names it (for example "env" or "shared-credentials-file"),
never the keys themselves.

The download is checked before it is kept: it goes to a temporary
"<output>.part" file, its size must equal the object's size in S3, and it
must parse as a JSON list of records.  Only then is it renamed to the output
name, so a failed run never leaves a half-written applicant_data_SM.json (an
older one stays as it was).

Exit codes of the command line: 0 downloaded, 1 the download failed (the
message says why).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import boto3
from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

# The bucket's name, in this one place: the notebook, the command line and the tests all
# read it.  S3 names are global, and "grad-cafe" and "grad-cafe-ms" belong to other
# accounts (README.md, section 2.4).
DEFAULT_BUCKET = "grad-cafe-msasso1"
DEFAULT_KEY = "applicant_data.json"
DEFAULT_OUTPUT = "applicant_data_SM.json"
BUCKET_VARIABLE = "GRAD_CAFE_BUCKET"   # names another bucket without editing any code

# S3 error codes and what they usually mean here.  A HEAD request has no response
# body, so a missing bucket and a missing object both arrive as a bare "404".
_ERROR_HINTS = {
    "404": "the bucket or the object does not exist (check the bucket name and the key)",
    "NoSuchKey": "the object does not exist in this bucket (check the key)",
    "NoSuchBucket": "the bucket does not exist (check the bucket name)",
    "403": "access denied: the IAM identity may not read this object (it needs "
           "s3:GetObject on it; without s3:ListBucket a missing object also looks like this)",
    "AccessDenied": "access denied: the IAM identity may not read this object",
    "InvalidAccessKeyId": "the access key ID is not valid (check the credentials)",
    "SignatureDoesNotMatch": "the secret access key does not match the access key ID",
    "ExpiredToken": "the temporary credentials have expired",
}


class S3FetchError(Exception):
    """A download that could not be done.  The message says why; it never contains
    a credential."""


@dataclass(frozen=True)
class FetchResult:
    """What one download did: where the data came from, where it went, and what it holds."""

    bucket: str
    key: str
    path: Path
    size_bytes: int
    records: int
    credential_source: str

    def describe(self) -> str:
        """A short report for the notebook or the terminal (no secrets in it)."""
        return "\n".join([
            f"Downloaded s3://{self.bucket}/{self.key}",
            f"Saved as   {self.path}",
            f"Size       {self.size_bytes:,} bytes",
            f"Records    {self.records:,}",
            f"Credentials came from boto3's default chain: {self.credential_source}",
        ])


def default_bucket() -> str:
    """The bucket to read: the GRAD_CAFE_BUCKET environment variable, else DEFAULT_BUCKET."""
    return os.environ.get(BUCKET_VARIABLE, "").strip() or DEFAULT_BUCKET


def make_session(profile: str | None = None, region: str | None = None) -> boto3.session.Session:
    """A boto3 session that uses the default credential chain.

    *profile* (a name from ~/.aws/config) and *region* are optional; None lets
    boto3 decide (AWS_PROFILE, AWS_DEFAULT_REGION, the config file, the instance)."""
    try:
        return boto3.session.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as err:
        raise S3FetchError(f"the AWS profile {profile!r} was not found in ~/.aws/config "
                           "or ~/.aws/credentials") from err


def credential_source(session: boto3.session.Session) -> str:
    """Which link of the credential chain supplied *session*'s credentials, e.g. "env",
    "shared-credentials-file" or "iam-role".  Raises S3FetchError if there are none."""
    try:
        credentials = session.get_credentials()
    except BotoCoreError as err:   # e.g. a profile that names a missing source profile
        raise S3FetchError(f"could not load AWS credentials: {err}") from err
    if credentials is None:
        raise S3FetchError(
            "no AWS credentials found.  On SageMaker, check that the notebook instance has an "
            "execution role; elsewhere run `aws configure` (or set AWS_PROFILE) first.")
    return credentials.method


def explain_client_error(err: ClientError, bucket: str, key: str) -> str:
    """A one-line explanation of an S3 error response for s3://bucket/key."""
    code = str(err.response.get("Error", {}).get("Code", "unknown"))
    hint = _ERROR_HINTS.get(code, "S3 refused the request")
    return f"s3://{bucket}/{key}: {hint} (S3 error code {code})"


def count_records(path: Path) -> int:
    """The number of records in the JSON file at *path*, which must hold a list of objects."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise S3FetchError(f"the downloaded file is not valid JSON: {err}") from err
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise S3FetchError("the downloaded file is JSON but not a list of applicant records")
    if not data:
        raise S3FetchError("the downloaded file holds no applicant records")
    return len(data)


def download_object(client, bucket: str, key: str, destination: Path) -> tuple[int, int]:
    """Download s3://bucket/key with *client* (a boto3 S3 client) to *destination*.

    The file is written to "<destination>.part" first, checked (size, JSON list)
    and only then renamed to *destination*.  Returns (size in bytes, number of
    records).  Raises S3FetchError for anything that went wrong."""
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")
    try:
        expected = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
        client.download_file(bucket, key, str(partial))
        size = partial.stat().st_size
        if size != expected:
            raise S3FetchError(f"s3://{bucket}/{key}: downloaded {size:,} bytes, "
                               f"but the object has {expected:,}")
        records = count_records(partial)
        os.replace(partial, destination)
    except ClientError as err:
        raise S3FetchError(explain_client_error(err, bucket, key)) from err
    except (BotoCoreError, Boto3Error) as err:   # no network, no credentials, retries used up
        raise S3FetchError(f"s3://{bucket}/{key}: {err}") from err
    except OSError as err:
        raise S3FetchError(f"could not save {destination}: {err}") from err
    finally:
        partial.unlink(missing_ok=True)
    return size, records


def fetch_applicant_data(bucket: str | None = None, key: str = DEFAULT_KEY,
                         output: str | Path = DEFAULT_OUTPUT,
                         session: boto3.session.Session | None = None) -> FetchResult:
    """Download s3://<bucket>/<key> and save it as *output* (applicant_data_SM.json).

    *bucket* defaults to default_bucket(), *session* to make_session().  A
    relative *output* is relative to the current folder; in Jupyter that is the
    notebook's own folder."""
    bucket = bucket or default_bucket()
    session = session or make_session()
    source = credential_source(session)
    client = session.client("s3")
    path = Path(output).resolve()
    size, records = download_object(client, bucket, key, path)
    return FetchResult(bucket=bucket, key=key, path=path, size_bytes=size,
                       records=records, credential_source=source)


def main(argv: list[str] | None = None) -> int:
    """Command line: download the applicant data; returns 0 or 1 (see the module docstring)."""
    parser = argparse.ArgumentParser(
        description="Download the Grad Café applicant data from S3 with boto3 and save it "
                    f"as {DEFAULT_OUTPUT}.",
        epilog="Credentials come from boto3's default chain (environment, ~/.aws, or the "
               "instance's IAM role); none are passed on the command line.",
    )
    parser.add_argument("--bucket", default=default_bucket(),
                        help=f"S3 bucket (default: ${BUCKET_VARIABLE} or {DEFAULT_BUCKET})")
    parser.add_argument("--key", default=DEFAULT_KEY,
                        help=f"object key in the bucket (default: {DEFAULT_KEY})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help=f"file to write (default: {DEFAULT_OUTPUT} in the current folder)")
    parser.add_argument("--profile", default=None,
                        help="AWS profile from ~/.aws/config (default: AWS_PROFILE or the default)")
    parser.add_argument("--region", default=None,
                        help="AWS region (default: AWS_DEFAULT_REGION or the profile's region)")
    args = parser.parse_args(argv)

    try:
        session = make_session(args.profile, args.region)
        result = fetch_applicant_data(args.bucket, args.key, args.output, session)
    except S3FetchError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(result.describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
