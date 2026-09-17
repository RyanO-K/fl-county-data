import recordings as R


def test_norm_name_uppercases_and_strips_punctuation():
    assert R.norm_name("O'Brien, Mary-Ann  ") == "O BRIEN MARY ANN"


def test_norm_name_drops_trailing_et_al_and_trustee():
    assert R.norm_name("SMITH JOHN ET AL") == "SMITH JOHN"
    assert R.norm_name("SMITH JOHN TRUSTEE") == "SMITH JOHN"
    assert R.norm_name("SMITH JOHN TR") == "SMITH JOHN"
    assert R.norm_name("SMITH JOHN ET UX") == "SMITH JOHN"


def test_norm_name_keeps_jr():
    assert R.norm_name("Fuller Robert Allen Jr") == "FULLER ROBERT ALLEN JR"


def test_norm_name_none_and_blank():
    assert R.norm_name(None) == ""
    assert R.norm_name("   ") == ""


def test_name_key_truncates_to_30():
    assert R.name_key("A" * 40) == "A" * 30
    assert R.name_key("Smith John") == "SMITH JOHN"


def test_categorize_rules():
    assert R.categorize("DEED") == "deed"
    assert R.categorize("WARRANTY DEED") == "deed"
    assert R.categorize("TAX DEED") == "deed"
    assert R.categorize("MORTGAGE") == "mortgage"
    assert R.categorize("MORTGAGE NO INTANGIBLE TAXES") == "mortgage"
    assert R.categorize("MORTGAGE2") == "mortgage"
    assert R.categorize("ASSIGNMENT OF MORTGAGE") == "assignment"
    assert R.categorize("SATISFACTION") == "satisfaction"
    assert R.categorize("SATISFACTION OF MORTGAGE") == "satisfaction"
    assert R.categorize("RELEASE LIS PENDENS") == "release"
    assert R.categorize("PARTIAL RELEASE") == "release"
    assert R.categorize("LIS PENDENS") == "lis_pendens"
    assert R.categorize("LIENX") == "lien"
    assert R.categorize("JUDGMENT") == "judgment"
    assert R.categorize("CERT COPY CRT JDGMNT") == "judgment"
    assert R.categorize("CERTIFIED COPY OF COURT JUDGMENT") == "judgment"
    assert R.categorize("NOTICE OF COMMENCEMENT") == "other"
    assert R.categorize("") == "other"
    assert R.categorize(None) == "other"
