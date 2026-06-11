import json
from types import SimpleNamespace

import pytest

from ledgerline import LedgerlineError
from ledgerline.categorize import (
    TAXONOMY,
    apply_cache,
    categorize_llm,
    categorize_rules_only,
    set_manual,
    unconfirmed,
)
from ledgerline.ingest import ingest_file
from tests.conftest import FIXTURES


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_rules_only_works_without_api_key(conn):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    applied, unknown = categorize_rules_only(conn)
    assert applied > 0
    cats = dict(
        conn.execute(
            "SELECT merchant_clean, category FROM transactions"
        ).fetchall()
    )
    assert cats["Kroger Springfield"] == "groceries"
    assert cats["Netflix.com"] == "subscriptions"
    assert cats["Duke Energy Electric Pmt"] == "utilities"
    assert cats["Payroll Acme Corp Direct Dep"] == "income"
    # niche merchants fall through to the LLM step
    assert "Zimberly Office Supply Co" in unknown
    assert "Brightstone Training Llc" in unknown


def test_llm_step_fails_loudly_without_key(conn):
    with pytest.raises(LedgerlineError, match="ANTHROPIC_API_KEY"):
        categorize_llm(conn, ["Some Merchant"])


def _fake_client(assignments):
    text_block = SimpleNamespace(type="text", text=json.dumps({"assignments": assignments}))
    response = SimpleNamespace(content=[text_block])
    messages = SimpleNamespace(create=lambda **kw: response)
    return SimpleNamespace(messages=messages)


def test_llm_batch_caches_and_applies(conn, monkeypatch):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    _, unknown = categorize_rules_only(conn)
    monkeypatch.setattr(
        "ledgerline.categorize.require_client",
        lambda: _fake_client(
            [
                {"merchant": "Zimberly Office Supply Co", "category": "professional"},
                {"merchant": "Brightstone Training Llc", "category": "professional"},
            ]
        ),
    )
    n = categorize_llm(conn, unknown)
    assert n == 2
    row = conn.execute(
        "SELECT category, source FROM merchant_category_cache"
        " WHERE merchant_clean = 'Brightstone Training Llc'"
    ).fetchone()
    assert row["category"] == "professional"
    assert row["source"] == "llm"


def test_llm_off_taxonomy_and_missing_merchants_become_other(conn, monkeypatch):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    _, unknown = categorize_rules_only(conn)
    monkeypatch.setattr(
        "ledgerline.categorize.require_client",
        lambda: _fake_client(
            [
                # invalid category must be rejected
                {"merchant": "Zimberly Office Supply Co", "category": "dentistry"},
                # hallucinated merchant must be ignored
                {"merchant": "Totally Invented Llc", "category": "dining"},
                # "Brightstone Training Llc" omitted entirely
            ]
        ),
    )
    categorize_llm(conn, unknown)
    rows = dict(
        conn.execute(
            "SELECT merchant_clean, category FROM merchant_category_cache"
            " WHERE merchant_clean IN ('Zimberly Office Supply Co', 'Brightstone Training Llc')"
        ).fetchall()
    )
    assert rows["Zimberly Office Supply Co"] == "other"
    assert rows["Brightstone Training Llc"] == "other"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM merchant_category_cache"
            " WHERE merchant_clean = 'Totally Invented Llc'"
        ).fetchone()[0]
        == 0
    )


def test_cache_means_one_llm_call_per_merchant_ever(conn, monkeypatch):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    _, unknown = categorize_rules_only(conn)
    monkeypatch.setattr(
        "ledgerline.categorize.require_client",
        lambda: _fake_client(
            [{"merchant": m, "category": "other"} for m in unknown]
        ),
    )
    categorize_llm(conn, unknown)
    # re-import the same file: everything resolves from cache, nothing uncached
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    _, unknown_again = categorize_rules_only(conn)
    assert unknown_again == []


def test_manual_correction_retroactive(conn, monkeypatch):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    _, unknown = categorize_rules_only(conn)
    monkeypatch.setattr(
        "ledgerline.categorize.require_client",
        lambda: _fake_client(
            [{"merchant": "Brightstone Training Llc", "category": "other"}]
        ),
    )
    categorize_llm(conn, ["Brightstone Training Llc"])
    n = set_manual(conn, "Brightstone Training Llc", "professional")
    assert n == 1
    row = conn.execute(
        "SELECT category FROM transactions WHERE merchant_clean = 'Brightstone Training Llc'"
    ).fetchone()
    assert row["category"] == "professional"
    cache = conn.execute(
        "SELECT source, confirmed FROM merchant_category_cache"
        " WHERE merchant_clean = 'Brightstone Training Llc'"
    ).fetchone()
    assert cache["source"] == "manual"
    assert cache["confirmed"] == 1


def test_unconfirmed_orders_llm_first(conn, monkeypatch):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    _, unknown = categorize_rules_only(conn)
    monkeypatch.setattr(
        "ledgerline.categorize.require_client",
        lambda: _fake_client([{"merchant": m, "category": "other"} for m in unknown]),
    )
    categorize_llm(conn, unknown)
    rows = unconfirmed(conn)
    assert rows[0]["source"] == "llm"


def test_taxonomy_is_flat_and_fixed():
    assert len(TAXONOMY) == len(set(TAXONOMY))
    assert "professional" in TAXONOMY
    assert "other" in TAXONOMY
