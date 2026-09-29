"""Tests for the "did you mean?" typo suggester (embeddings/did_you_mean.py)."""

from aosphere_core_index.embeddings.did_you_mean import build_lexicon, suggest

# Lexicon shaped like the real one: titles + body text + alias/regulator vocab.
LEX = build_lexicon(
    titles=[
        "Cookies", "Consent requirements", "Direct marketing",
        "Netting of positions", "Transparency of Ultimate Beneficial Ownership",
        "Processing children's data", "Data breach notification", "Disclosure",
    ],
    extra=["PIPC", "PICP", "United Kingdom", "South Korea", "Germany", "CNMV"],
    bodies=[
        # bigrams + frequency signal ("cookie walls" attested; PIPC dominant).
        # Body words enter the vocabulary at freq >= 3; bigrams attest at >= 2.
        "Consent obtained via cookie walls is not valid. Cookie walls require "
        "freely given consent. Cookie walls block access. Cookie banners and "
        "cookie policies are common. "
        "A substantial holding must be disclosed. The substantial holding "
        "threshold applies. A substantial holding notification is required. "
        "You must disclose the holding. Disclose promptly. Disclose changes. "
        "Interests are disclosable. Positions are disclosable. Swaps are "
        "disclosable instruments. "
        "The PIPC must be notified. The PIPC issues guidance. The PIPC decides.",
    ],
)


def test_bigram_repair_real_word_typo():
    # "cooker" is a valid English word — caught via the attested "cookie walls" bigram.
    assert suggest("Can we use cooker walls in Germany?", LEX) == \
        "Can we use cookie walls in Germany?"


def test_acronym_transposition_freq_tiebreak():
    # PCPI anagrams to both PIPC and PICP; corpus frequency picks PIPC.
    assert suggest("Do we need to notify the PCPI?", LEX) == \
        "Do we need to notify the PIPC?"


def test_unknown_word_close_match_with_bigram_context():
    # Both tokens are typos; corrected left neighbour steers "hilding" to
    # "holding" (attested "substantial holding"), not another near word.
    assert suggest("a substsntial hilding in Germany", LEX) == \
        "a substantial holding in Germany"


def test_inflection_suggestion_allowed_for_nonwords():
    assert suggest("Disclos the holding", LEX) == "Disclose the holding"


def test_closer_word_beats_attested_but_distant_one():
    # "disclosble" is far closer to "disclosable" (0.95) than to "disclose"
    # (0.89): the bigram-attestation bonus must not override a clearly better
    # ratio, and stopword neighbours ("in") must not attest anything.
    assert suggest("Are CFDs disclosble in Germany", LEX) == \
        "Are CFDs disclosable in Germany"


def test_clean_query_no_suggestion():
    assert suggest("Do you need consent for direct marketing?", LEX) is None


def test_english_words_not_in_domain_left_alone():
    # "publish", "deadline", "world" are common English missing from the domain
    # lexicon — must NOT be "corrected" to nearby domain words.
    assert suggest("What is the deadline to publish this in the world?", LEX) is None


def test_stopword_neighbours_never_trigger_bigram_repair():
    assert suggest("Is there a deadline for disclosure?", LEX) is None


def test_hyphenated_compounds_known():
    assert suggest("Is there a soft opt-in rule?", LEX) is None


def test_acronyms_never_fuzzy_matched():
    # CNMV-adjacent acronyms must not be edit-distance corrected.
    assert suggest("Are CFDs disclosable?", LEX) is None


def test_clause_keys_and_digits_untouched():
    assert suggest("What does A3.2 say about netting?", LEX) is None


def test_capitalization_preserved():
    s = suggest("Consnet requirements in Germany", LEX)
    assert s == "Consent requirements in Germany"


def test_empty_inputs():
    assert suggest("", LEX) is None
    assert suggest("anything", build_lexicon([])) is None
