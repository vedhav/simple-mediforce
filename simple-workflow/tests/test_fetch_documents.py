"""Behaviour test for scripts/fetch_study_documents.py.

Runs the real script file against the fixture step input, pointing its output
directory at a scratch dir (MEDIFORCE_OUTPUT_DIR) the way local mode redirects
`/output`. Asserts the documented result.json shape and that the PDFs actually
landed on disk.

Needs outbound network to clinicaltrials.gov. Exits 2 (SKIP) when the API is
unreachable rather than reporting a failure the code did not cause.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parent.parent
SCRIPT = WORKFLOW_DIR / "scripts" / "fetch_study_documents.py"
FIXTURE = WORKFLOW_DIR / "tests" / "fixtures" / "fetch-documents.input.json"
SKIP_EXIT_CODE = 2


def network_available() -> bool:
    try:
        urllib.request.urlopen("https://clinicaltrials.gov/api/v2/version", timeout=15).read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def run_script(output_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={**os.environ, "MEDIFORCE_OUTPUT_DIR": str(output_dir)},
        capture_output=True,
        text=True,
    )


def test_downloads_every_posted_document() -> None:
    output_dir = Path(tempfile.mkdtemp(prefix="fetch-documents-"))
    try:
        shutil.copyfile(FIXTURE, output_dir / "input.json")
        completed = run_script(output_dir)
        assert completed.returncode == 0, (
            f"script exited {completed.returncode}\nstdout: {completed.stdout}\nstderr: {completed.stderr}"
        )

        result = json.loads((output_dir / "result.json").read_text())
        expected_nct_id = json.loads(FIXTURE.read_text())["steps"]["select-study"]["nctId"]

        assert result["nctId"] == expected_nct_id, result
        assert result["documentCount"] == len(result["documents"]), result
        assert result["documentCount"] > 0, "fixture study should have posted documents"
        assert expected_nct_id in result["summary"], result["summary"]

        for document in result["documents"]:
            for key in (
                "filename", "sourceFilename", "sourceUrl", "typeAbbrev", "label",
                "date", "hasProtocol", "hasSap", "hasIcf", "sizeBytes",
            ):
                assert key in document, f"missing '{key}' in {document}"

            downloaded = output_dir / document["filename"]
            assert downloaded.is_file(), f"{downloaded} was not written"
            assert downloaded.stat().st_size == document["sizeBytes"], (
                f"{document['filename']}: on-disk {downloaded.stat().st_size} != "
                f"reported {document['sizeBytes']}"
            )
            assert downloaded.read_bytes()[:5] == b"%PDF-", f"{document['filename']} is not a PDF"
            assert document["filename"].startswith(f"{expected_nct_id}_"), document["filename"]

        types = sorted(d["typeAbbrev"] for d in result["documents"])
        print(f"  downloaded {result['documentCount']} document(s): {types}")
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def test_missing_selection_fails_with_reported_error() -> None:
    """No nctId in step input is a hard failure, reported through result.json."""
    output_dir = Path(tempfile.mkdtemp(prefix="fetch-documents-nosel-"))
    try:
        (output_dir / "input.json").write_text(json.dumps({"steps": {}}))
        completed = run_script(output_dir)

        assert completed.returncode == 1, f"expected exit 1, got {completed.returncode}"
        result = json.loads((output_dir / "result.json").read_text())
        assert "nctId" in result["error"], result
        print("  missing-selection path reported: " + result["error"])
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def main() -> int:
    if not network_available():
        print("SKIP — clinicaltrials.gov unreachable (needs outbound network)")
        return SKIP_EXIT_CODE

    test_missing_selection_fails_with_reported_error()
    test_downloads_every_posted_document()
    print("PASS — fetch_study_documents.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
