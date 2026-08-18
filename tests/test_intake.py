"""
tests/test_intake.py — Unit tests for the LLM-driven intake helpers.

Tests the pure logic used by the natural-conversation intake flow:
field merging and lead-readiness detection.

Run: uv run pytest tests/test_intake.py -v -s
"""

from __future__ import annotations

from services.intake import merge_fields, is_lead_ready


def test_merge_fields_merges_non_empty_only():
    data = {}
    merge_fields(
        data,
        {"service": "Web App", "description": "", "budget": " 5jt+ ", "contact": ""},
    )
    assert data["service"] == "Web App"
    assert data["budget"] == "5jt+"
    assert "description" not in data or not data.get("description")
    assert "contact" not in data or not data.get("contact")


def test_merge_fields_preserves_existing_values():
    data = {"service": "Web App", "description": "sudah ada"}
    merge_fields(data, {"service": "", "description": "deskripsi baru"})
    assert data["service"] == "Web App"  # empty new value does not overwrite
    assert data["description"] == "deskripsi baru"


def test_is_lead_ready_requires_essentials_plus_optional():
    empty = {}
    assert is_lead_ready(empty) is False

    only_essentials = {
        "service": "Web App",
        "description": "aplikasi absensi",
        "contact": "0812",
    }
    # missing both budget and deadline
    assert is_lead_ready(only_essentials) is False

    ready_with_budget = {
        "service": "Web App",
        "description": "aplikasi absensi",
        "contact": "0812",
        "budget": "2 juta",
    }
    assert is_lead_ready(ready_with_budget) is True

    ready_with_deadline = {
        "service": "Web App",
        "description": "aplikasi absensi",
        "contact": "0812",
        "deadline": "1 bulan",
    }
    assert is_lead_ready(ready_with_deadline) is True

    missing_contact = {
        "service": "Web App",
        "description": "aplikasi absensi",
        "budget": "2 juta",
    }
    assert is_lead_ready(missing_contact) is False
