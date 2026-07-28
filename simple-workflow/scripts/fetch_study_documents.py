"""Download every posted document for one ClinicalTrials.gov study.

Reads the NCT id chosen by the `select-study` human step from
`/output/input.json`, asks the ClinicalTrials.gov v2 API which large documents
the study has posted, downloads each one into the step's output directory, and
writes `/output/result.json`.

Files left in the output directory (other than the engine's own control files)
become durable Output Files on the run branch, so the PDFs are the deliverable
of this step and the JSON is its machine-readable index.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

OUTPUT_DIR = Path(os.environ.get("MEDIFORCE_OUTPUT_DIR", "/output"))
STUDY_API = "https://clinicaltrials.gov/api/v2/studies/{nct_id}?fields=documentSection"
DOCUMENT_CDN = "https://cdn.clinicaltrials.gov/large-docs/{shard}/{nct_id}/{filename}"
REQUEST_TIMEOUT_SECONDS = 120
DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3


def fail(message: str) -> NoReturn:
    """Report a step failure the way the script-container plugin reads it.

    The plugin surfaces `result.json`'s `error` field when a script exits
    non-zero without printing (script-container-plugin.ts readResultError), so
    write the file before exiting.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "result.json").write_text(json.dumps({"error": message}, indent=2))
    print(message, file=sys.stderr)
    sys.exit(1)


def read_selected_nct_id() -> str:
    input_path = OUTPUT_DIR / "input.json"
    try:
        step_input = json.loads(input_path.read_text())
    except FileNotFoundError:
        fail(f"{input_path} not found — the engine did not provide step input")
    except json.JSONDecodeError as error:
        fail(f"{input_path} is not valid JSON: {error}")

    # The engine builds step input as `{...previousStepOutput, steps: variables}`,
    # so the selection is readable either by step id or from the flattened
    # predecessor output.
    by_step = (step_input.get("steps") or {}).get("select-study") or {}
    nct_id = by_step.get("nctId") or step_input.get("nctId")
    if not isinstance(nct_id, str) or nct_id == "":
        fail("no nctId in step input — expected steps['select-study'].nctId")
    return nct_id


def fetch_document_metadata(nct_id: str) -> list[dict]:
    url = STUDY_API.format(nct_id=nct_id)
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            study = json.load(response)
    except urllib.error.HTTPError as error:
        fail(f"ClinicalTrials.gov returned HTTP {error.code} for {nct_id}")
    except (urllib.error.URLError, TimeoutError) as error:
        fail(f"could not reach ClinicalTrials.gov for {nct_id}: {error}")
    except json.JSONDecodeError as error:
        fail(f"ClinicalTrials.gov returned malformed JSON for {nct_id}: {error}")

    document_section = study.get("documentSection") or {}
    large_document_module = document_section.get("largeDocumentModule") or {}
    return large_document_module.get("largeDocs") or []


def document_url(nct_id: str, filename: str) -> str:
    """Build the CDN URL. Documents are sharded by the id's last two characters."""
    return DOCUMENT_CDN.format(shard=nct_id[-2:], nct_id=nct_id, filename=filename)


def download(url: str, destination: Path) -> int:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read()
            destination.write_bytes(payload)
            return len(payload)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            print(f"attempt {attempt}/{DOWNLOAD_ATTEMPTS} failed for {url}: {error}", file=sys.stderr)
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    fail(f"could not download {url} after {DOWNLOAD_ATTEMPTS} attempts: {last_error}")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    nct_id = read_selected_nct_id()
    print(f"fetching document metadata for {nct_id}")

    large_docs = fetch_document_metadata(nct_id)
    print(f"study lists {len(large_docs)} document(s)")

    documents = []
    for large_doc in large_docs:
        filename = large_doc.get("filename")
        if not filename:
            fail(f"a document entry for {nct_id} has no filename: {large_doc}")

        source_url = document_url(nct_id, filename)
        local_name = f"{nct_id}_{filename}"
        size_bytes = download(source_url, OUTPUT_DIR / local_name)
        print(f"downloaded {local_name} ({size_bytes} bytes)")

        documents.append({
            "filename": local_name,
            "sourceFilename": filename,
            "sourceUrl": source_url,
            "typeAbbrev": large_doc.get("typeAbbrev"),
            "label": large_doc.get("label"),
            "date": large_doc.get("date"),
            "hasProtocol": bool(large_doc.get("hasProtocol")),
            "hasSap": bool(large_doc.get("hasSap")),
            "hasIcf": bool(large_doc.get("hasIcf")),
            "sizeBytes": size_bytes,
        })

    result = {
        "nctId": nct_id,
        "documentCount": len(documents),
        "documents": documents,
        "summary": (
            f"Downloaded {len(documents)} document(s) for {nct_id}"
            if documents
            else f"{nct_id} has no posted documents"
        ),
    }
    (OUTPUT_DIR / "result.json").write_text(json.dumps(result, indent=2))
    print(result["summary"])


if __name__ == "__main__":
    main()
