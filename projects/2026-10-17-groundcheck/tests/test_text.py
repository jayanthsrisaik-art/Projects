from groundcheck.text import (
    antonym_clash, content_tokens, negation_count, numbers, proper_nouns, split_sentences,
)


def test_split_sentences_handles_am_and_numbers():
    s = split_sentences("It opens at 8 a.m. Entry costs 12 dollars. Peak was 4.8 metres on March 14. Done!")
    assert s == ["It opens at 8 a.m.", "Entry costs 12 dollars.", "Peak was 4.8 metres on March 14.", "Done!"]


def test_numbers_normalised():
    assert numbers("2,140 metres and 0.250 dollars, in 2024.") == {"2140", "0.25", "2024"}


def test_content_tokens_drop_stopwords_and_negations_and_stem():
    assert content_tokens("The dividends were not paid") == {"dividend", "paid"}


def test_negation_count_contractions():
    assert negation_count("It doesn't work") == 1
    assert negation_count("It works") == 0


def test_proper_nouns_skip_sentence_initial():
    assert proper_nouns("Mount Alder opened in March") == {"alder", "march"}


def test_antonym_clash_is_directional_to_mismatch():
    assert antonym_clash("The bridge was closed", "The bridge remained open")
    assert not antonym_clash("The bridge was open", "The bridge remained open")
