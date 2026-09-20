from app.rag.normalize import normalize, tokenize


def test_greek_accents_are_removed() -> None:
    assert normalize("Ροτόντα") == "ροτοντα"
    assert normalize("Ροτόντα") == normalize("Ροτοντα")


def test_final_sigma_is_canonicalized() -> None:
    assert normalize("Θεσσαλονίκης") == "θεσσαλονικησ"
    assert normalize("Θεσσαλονίκης") == normalize("θεσσαλονικησ")


def test_latin_combining_marks_are_removed() -> None:
    assert normalize("Café") == "cafe"


def test_ascii_is_unchanged_apart_from_case_punctuation_and_spacing() -> None:
    assert normalize("  Roman, MARKET!  ") == "roman market"
    assert tokenize("  Roman, MARKET!  ") == ["roman", "market"]
