"""Operator-owned acceptance tests for the fixed, offline CRM reference case."""

import json
import sqlite3
import subprocess
import sys

import pytest
from crm import CRM


def assert_dictionary_rows(rows):
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)


def test_empty_database(tmp_path):
    crm = CRM(str(tmp_path / "crm.sqlite"))
    try:
        assert crm.list_customers() == []
        assert crm.list_events() == []
    finally:
        crm.close()


def test_customer_survives_reopening(tmp_path):
    path = str(tmp_path / "crm.sqlite")
    crm = CRM(path)
    customer = crm.add_customer("Émilie O'Connor", "emilie@example.invalid")
    assert type(customer) is int
    crm.close()
    assert (tmp_path / "crm.sqlite").read_bytes().startswith(b"SQLite format 3\x00")
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    # A new interpreter cannot recover data from a per-process fake store.
    reopened = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from crm import CRM; c=CRM(sys.argv[1]); "
                "print(json.dumps(c.list_customers())); c.close()"
            ),
            path,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(reopened.stdout) == [
        {"id": customer, "name": "Émilie O'Connor", "email": "emilie@example.invalid"}
    ]
    crm = CRM(path)
    try:
        rows = crm.list_customers()
        assert_dictionary_rows(rows)
        assert len(rows) == 1
        assert rows[0]["id"] == customer
        assert rows[0]["name"] == "Émilie O'Connor"
        assert rows[0]["email"] == "emilie@example.invalid"
    finally:
        crm.close()


@pytest.mark.parametrize("name", ["", " ", "\t\n"])
def test_blank_customer_name_is_rejected(tmp_path, name):
    crm = CRM(str(tmp_path / "crm.sqlite"))
    try:
        with pytest.raises(ValueError):
            crm.add_customer(name, "nobody@example.invalid")
        assert crm.list_customers() == []
    finally:
        crm.close()


def test_quotes_are_persistent_and_scoped_to_customer(tmp_path):
    path = str(tmp_path / "crm.sqlite")
    crm = CRM(path)
    first = crm.add_customer("First", "first@example.invalid")
    second = crm.add_customer("Second", "second@example.invalid")
    quote = crm.add_quote(first, "Conseil d'équipe", 1499)
    assert type(quote) is int
    crm.close()
    crm = CRM(path)
    try:
        rows = crm.list_quotes(first)
        assert_dictionary_rows(rows)
        assert len(rows) == 1
        assert rows[0]["id"] == quote
        assert rows[0]["customer_id"] == first
        assert rows[0]["description"] == "Conseil d'équipe"
        assert rows[0]["total_cents"] == 1499
        assert crm.list_quotes(second) == []
    finally:
        crm.close()


def test_negative_quote_is_rejected(tmp_path):
    crm = CRM(str(tmp_path / "crm.sqlite"))
    try:
        customer = crm.add_customer("First", "first@example.invalid")
        with pytest.raises(ValueError):
            crm.add_quote(customer, "Invalid", -1)
        assert crm.list_quotes(customer) == []
    finally:
        crm.close()


@pytest.mark.parametrize("operation", ["quote", "draft"])
def test_unknown_customer_is_rejected(tmp_path, operation):
    crm = CRM(str(tmp_path / "crm.sqlite"))
    try:
        with pytest.raises(ValueError):
            if operation == "quote":
                crm.add_quote(99999, "Invalid", 100)
            else:
                crm.draft_email(99999, "Invalid", "No recipient")
    finally:
        crm.close()


def test_event_survives_reopening(tmp_path):
    path = str(tmp_path / "crm.sqlite")
    crm = CRM(path)
    event = crm.add_event("Réunion d'équipe", "2026-10-01T13:00:00-04:00")
    assert type(event) is int
    crm.close()
    crm = CRM(path)
    try:
        rows = crm.list_events()
        assert_dictionary_rows(rows)
        assert len(rows) == 1
        assert rows[0]["id"] == event
        assert rows[0]["title"] == "Réunion d'équipe"
        assert rows[0]["starts_at"] == "2026-10-01T13:00:00-04:00"
    finally:
        crm.close()


def test_drafts_are_persistent_and_scoped_to_customer(tmp_path):
    path = str(tmp_path / "crm.sqlite")
    crm = CRM(path)
    first = crm.add_customer("First", "first@example.invalid")
    second = crm.add_customer("Second", "second@example.invalid")
    draft = crm.draft_email(first, "Suivi d'équipe", "Bonjour,\nMerci !")
    assert type(draft) is int
    crm.close()
    crm = CRM(path)
    try:
        rows = crm.list_email_drafts(first)
        assert_dictionary_rows(rows)
        assert len(rows) == 1
        assert rows[0]["id"] == draft
        assert rows[0]["customer_id"] == first
        assert rows[0]["subject"] == "Suivi d'équipe"
        assert rows[0]["body"] == "Bonjour,\nMerci !"
        assert crm.list_email_drafts(second) == []
    finally:
        crm.close()


def test_apostrophes_are_data(tmp_path):
    crm = CRM(str(tmp_path / "crm.sqlite"))
    try:
        text = "Robert'); DROP TABLE customers; --"
        crm.add_customer(text, "first@example.invalid")
        crm.add_customer("Other", "other@example.invalid")
        assert {row["name"] for row in crm.list_customers()} == {text, "Other"}
    finally:
        crm.close()
