import pytest

from ledgerline.money import format_cents, parse_amount_to_cents


def test_basic_amounts():
    assert parse_amount_to_cents("1,234.56") == 123456
    assert parse_amount_to_cents("$12") == 1200
    assert parse_amount_to_cents("-3.5") == -350
    assert parse_amount_to_cents("0.01") == 1


def test_accounting_negative():
    assert parse_amount_to_cents("(45.00)") == -4500


def test_sign_convention_flip():
    # banks that export debits as positive
    assert parse_amount_to_cents("28.75", sign=-1) == -2875


def test_rejects_subcent_and_garbage():
    with pytest.raises(ValueError):
        parse_amount_to_cents("12.345")
    with pytest.raises(ValueError):
        parse_amount_to_cents("abc")
    with pytest.raises(ValueError):
        parse_amount_to_cents("")


def test_format_cents():
    assert format_cents(-123456) == "-$1,234.56"
    assert format_cents(5) == "$0.05"
