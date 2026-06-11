from ledgerline.normalize import clean_merchant


def test_square_prefix():
    assert clean_merchant("SQ *BRASS BADGER COFFEE") == "Brass Badger Coffee"


def test_toast_prefix_and_city_state():
    assert clean_merchant("TST* RIVER CITY DINER SPRINGFIELD IL") == "River City Diner Springfield"


def test_paypal_prefix():
    assert clean_merchant("PAYPAL *DIGITALRIVER") == "Digitalriver"


def test_store_number_stripped():
    assert clean_merchant("KROGER #423 SPRINGFIELD IL") == "Kroger Springfield"
    assert clean_merchant("CHIPOTLE 1278 SPRINGFIELD IL") == "Chipotle Springfield"


def test_trailing_reference_code():
    assert clean_merchant("AMAZON.COM*RT4Y7") == "Amazon.com"


def test_apostrophes_survive_title_case():
    assert clean_merchant("TRADER JOE'S #729") == "Trader Joe's"


def test_whitespace_collapsed():
    assert clean_merchant("  DUKE   ENERGY  ELECTRIC PMT ") == "Duke Energy Electric Pmt"


def test_short_names_keep_state_like_tokens():
    # only strip a trailing state code when there are enough leading tokens
    assert clean_merchant("GA POWER") == "Ga Power"


def test_deterministic():
    raw = "CHECKCARD 0119 DELTA AIR LINES SPRINGFIELD"
    assert clean_merchant(raw) == clean_merchant(raw)
