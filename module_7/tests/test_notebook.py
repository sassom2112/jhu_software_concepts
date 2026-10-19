"""
The notebook's code runs end to end against the fake S3, as plain Python.

The code cells are written, in order, into one script in a temporary folder
and run there with runpy, so applicant_data_SM.json lands in that folder just
as it lands next to the notebook in SageMaker.  This needs no Jupyter
installation, so the cells must be plain Python (no %magic or !shell lines).
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from s3_fetch import DEFAULT_BUCKET

NOTEBOOK = Path(__file__).resolve().parents[1] / "grad-cafe-pipeline.ipynb"


def _code_cells() -> list[str]:
    """The source of every code cell, in order."""
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]


def test_the_notebook_has_plain_python_cells():
    """No IPython magics or shell escapes, so nbqa pylint and this test see real code."""
    cells = _code_cells()
    assert cells
    for source in cells:
        for line in source.splitlines():
            assert not line.lstrip().startswith(("%", "!")), line


def test_the_notebook_imports_s3_fetch_instead_of_holding_boto3_code():
    """The boto3 logic lives in s3_fetch.py; the notebook only imports and calls it."""
    code = "\n".join(_code_cells())
    assert "from s3_fetch import" in code
    assert "import boto3" not in code and "boto3.client" not in code
    assert "aws_access_key_id" not in code.lower()
    assert "aws_secret_access_key" not in code.lower()


@pytest.mark.usefixtures("s3")
def test_the_notebook_runs_and_writes_applicant_data_sm(tmp_path, monkeypatch, capsys,
                                                       sample_body):
    """Run all cells; applicant_data_SM.json appears in the working folder."""
    script = tmp_path / "notebook_cells.py"
    script.write_text("\n\n".join(_code_cells()) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runpy.run_path(str(script), run_name="__main__")
    assert (tmp_path / "applicant_data_SM.json").read_bytes() == sample_body
    printed = capsys.readouterr().out
    assert f"Source: s3://{DEFAULT_BUCKET}/applicant_data.json" in printed
    assert "Records    3" in printed
    assert "3 records in applicant_data_SM.json" in printed
    assert "Accepted: 2" in printed
