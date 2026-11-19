"""Tests for release provenance attachments in .github/workflows/publish.yml (issue #469).

OpenSSF Scorecard requires provenance / signature files (*.intoto.jsonl, *.sigstore.json)
attached as release assets next to each artifact on GitHub Releases for the Signed-Releases check.
These tests verify that publish.yml defines the required permissions, attestation steps,
and bundle upload commands for all release artifacts.
"""

import re
from pathlib import Path

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish.yml"


def test_publish_workflow_file_exists():
    assert WORKFLOW_PATH.is_file(), f"Workflow file not found at {WORKFLOW_PATH}"


def test_build_deb_attaches_provenance_bundle():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Locate build-deb job
    deb_job_match = re.search(
        r"\n  build-deb:\n(.*?)(?=\n  [a-zA-Z0-9_-]+:|\Z)", content, re.DOTALL
    )
    assert deb_job_match, "build-deb job not found in publish.yml"
    deb_job = deb_job_match.group(1)

    # Check required permissions for OIDC attestation and release upload
    assert re.search(r"id-token:\s*write", deb_job), "build-deb requires id-token: write permission"
    assert re.search(r"attestations:\s*write", deb_job), (
        "build-deb requires attestations: write permission"
    )
    assert re.search(r"contents:\s*write", deb_job), "build-deb requires contents: write permission"

    # Check attestation step
    attest_match = re.search(
        r"-\s*name:[^\n]*\n\s+id:\s*([a-zA-Z0-9_-]+)\n\s+uses:\s*actions/attest-build-provenance[^\n]*\n\s+with:\n\s+subject-path:\s*['\"]taken_\*_all\.deb['\"]",
        deb_job,
    )
    assert attest_match, "Attest build provenance step for .deb with step id not found"
    step_id = attest_match.group(1)

    # Check that the bundle-path output is captured
    bundle_ref = f"${{{{ steps.{step_id}.outputs.bundle-path }}}}"
    assert bundle_ref in deb_job, f"Expected {bundle_ref} reference in build-deb job"

    # Check that the bundle is named with .intoto.jsonl (or .sigstore.json) and uploaded
    assert 'BUNDLE="${DEB}.intoto.jsonl"' in deb_job
    assert 'cp "$BUNDLE_PATH" "$BUNDLE"' in deb_job
    assert 'gh release upload "$TAG" "$DEB" "$BUNDLE" --clobber' in deb_job


def test_build_windows_attaches_provenance_bundle():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Locate build-windows job
    win_job_match = re.search(
        r"\n  build-windows:\n(.*?)(?=\n  [a-zA-Z0-9_-]+:|\Z)", content, re.DOTALL
    )
    assert win_job_match, "build-windows job not found in publish.yml"
    win_job = win_job_match.group(1)

    # Check required permissions
    assert re.search(r"id-token:\s*write", win_job), (
        "build-windows requires id-token: write permission"
    )
    assert re.search(r"attestations:\s*write", win_job), (
        "build-windows requires attestations: write permission"
    )
    assert re.search(r"contents:\s*write", win_job), (
        "build-windows requires contents: write permission"
    )

    # Check attestation step
    attest_match = re.search(
        r"-\s*name:[^\n]*\n\s+id:\s*([a-zA-Z0-9_-]+)\n\s+uses:\s*actions/attest-build-provenance[^\n]*\n\s+with:\n\s+subject-path:\s*['\"]taken_\*_windows-x64\.zip['\"]",
        win_job,
    )
    assert attest_match, "Attest build provenance step for Windows zip with step id not found"
    step_id = attest_match.group(1)

    # Check bundle-path reference
    bundle_ref = f"${{{{ steps.{step_id}.outputs.bundle-path }}}}"
    assert bundle_ref in win_job, f"Expected {bundle_ref} reference in build-windows job"

    # Check bundle naming and upload
    assert 'BUNDLE="${ZIP}.intoto.jsonl"' in win_job
    assert 'cp "$BUNDLE_PATH" "$BUNDLE"' in win_job
    assert 'gh release upload "$TAG" "$ZIP" "$BUNDLE" --clobber' in win_job
