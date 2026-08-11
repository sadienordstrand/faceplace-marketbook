#!/usr/bin/env python3
"""
The row filters, without a browser.

    python3 -m unittest tests.test_listings

Covers which query words a listing has to have, how several OR'd queries are
matched, reading a model year out of a title, and the bounds that act on it. The
titles here are the shapes real Marketplace listings come in — a year in front,
no year at all, a part number that looks like one — because that's where this
gets decided, not in the arithmetic afterwards.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import listings


def row(title, price="$20,000"):
    return {"source_section": "search", "matches_query": "yes", "title": title,
            "price": price, "raw_text": title}


def row_from(card):
    """The merged row a scrolled card turns into, so a test can hold the probe's
    answer up against the real filter's answer for the same listing."""
    title, price, _loc, _miles, lines = listings.parse_card_text(card["text"])
    return {"source_section": "search", "matches_query": "yes", "title": title,
            "price": price, "raw_text": " | ".join(lines)}


class QueryWordsTest(unittest.TestCase):
    """Every word of a query is required, and a number or a two-letter word is
    as much a word as any other: someone who typed "defender 110" does not want
    the 90s, and "vw bus" is not a search for buses."""

    def test_numbers_and_short_words_are_words(self):
        self.assertEqual(listings.query_tokens("Land Rover Defender 110"),
                         ["land", "rover", "defender", "110"])
        self.assertEqual(listings.query_tokens("VW bus"), ["vw", "bus"])
        self.assertEqual(listings.query_tokens("jeep 4x4"), ["jeep", "4x4"])
        self.assertEqual(listings.query_tokens(""), [])

    def matches(self, queries, *texts):
        return listings.matches_query(listings.query_groups(queries), *texts)

    def test_a_number_in_the_query_now_decides_what_is_kept(self):
        self.assertTrue(self.matches("defender 110", "1994 Defender 110"))
        self.assertFalse(self.matches("defender 110", "1994 Defender 90"))

    def test_a_two_letter_word_has_to_be_there_too(self):
        self.assertTrue(self.matches("vw bus", "1971 VW Bus"))
        self.assertFalse(self.matches("vw bus", "1971 Bus"))

    def test_words_still_match_at_their_start_only(self):
        self.assertTrue(self.matches("chev", "Chevrolet Silverado"))
        self.assertTrue(self.matches("chev", "1972 Chevy C10"))
        self.assertFalse(self.matches("van", "Advantage trailer"))

    def test_a_number_matches_at_a_word_start_as_well(self):
        # The price sits behind a '$' and the year in front of the model, and
        # neither one is a word this can hide inside.
        self.assertTrue(self.matches("110", "Defender 110, $12,000"))
        self.assertFalse(self.matches("110", "Defender, 4110 miles"))

    def test_several_queries_are_ored(self):
        both = ["defender 110", "land rover 90"]
        self.assertTrue(self.matches(both, "1994 Defender 110"))
        self.assertTrue(self.matches(both, "Land Rover 90 soft top"))
        # Half of each is not a match for either.
        self.assertFalse(self.matches(both, "1994 Land Rover 110"))
        self.assertFalse(self.matches(both, "Ford Bronco"))

    def test_blank_queries_are_dropped_rather_than_matching_everything(self):
        self.assertEqual(listings.query_list(["defender", "  ", ""]), ["defender"])
        self.assertEqual(listings.query_groups(["defender", " ", ","]),
                         [["defender"]])
        self.assertFalse(self.matches(["defender", ""], "Ford Bronco"))

    def test_asking_for_nothing_matches_everything(self):
        # An empty query is not a filter that rejects every listing there is.
        self.assertTrue(listings.matches_query([], "anything at all"))

    def test_the_whole_search_on_one_line(self):
        self.assertEqual(listings.query_label(["defender 110", "land rover 90"]),
                         "defender 110 OR land rover 90")
        self.assertEqual(listings.query_label("defender 110"), "defender 110")

    def test_the_words_are_read_from_the_card_text_as_well_as_the_title(self):
        groups = listings.query_groups("defender 110")
        self.assertTrue(listings.matches_query(groups, "Land Rover",
                                               "Land Rover | Defender 110"))


class RankingTest(unittest.TestCase):
    """Relevance decides which listings get a description first, so it reads the
    title on its own — every kept listing already has the words somewhere."""

    def score(self, title, queries="defender 110", **kw):
        r = {"title": title, "price": "$40,000", "source_section": "search", **kw}
        return listings.relevance(r, listings.query_groups(queries),
                                  listings.query_numbers(queries))

    def test_a_whole_query_in_the_title_outranks_one_only_in_the_card(self):
        self.assertGreater(self.score("1994 Land Rover Defender 110"),
                           self.score("Land Rover, ask for details"))

    def test_any_one_of_several_queries_counts(self):
        both = ["defender 110", "land rover 90"]
        self.assertEqual(self.score("1994 Defender 110", both),
                         self.score("1994 Land Rover 90", both))

    def test_numbers_are_not_counted_twice_when_two_queries_share_one(self):
        self.assertEqual(listings.query_numbers(["defender 110", "rover 110"]),
                         ["110"])


class BetterRowTest(unittest.TestCase):
    """Two of a search's queries can both turn up the same listing, in feeds
    ordered differently, so the two sightings have to reduce to one row."""

    def row(self, **kw):
        return {"source_section": "search", "matches_query": "yes",
                "title": "1994 Defender 110", "raw_text": "1994 Defender 110",
                **kw}

    def test_inside_the_radius_beats_past_the_divider(self):
        inside, outside = self.row(), self.row(source_section="outside_search")
        self.assertIs(listings.better_row(outside, inside), inside)
        self.assertIs(listings.better_row(inside, outside), inside)

    def test_a_card_that_rendered_beats_one_that_had_not(self):
        full = self.row()
        bare = self.row(title="", raw_text="", matches_query="no")
        self.assertIs(listings.better_row(bare, full), full)
        self.assertIs(listings.better_row(full, bare), full)

    def test_two_equally_good_sightings_keep_the_first(self):
        first, second = self.row(), self.row()
        self.assertIs(listings.better_row(first, second), first)


class ExcludeTest(unittest.TestCase):
    """One exclude term has to cover the spellings of the same words without
    swallowing longer words that merely contain them. Every string here is a
    real Marketplace title, or the shape of one."""

    def drops(self, term, title):
        return listings.is_excluded({"title": title, "raw_text": ""}, [term])

    def test_it_ignores_case_and_punctuation_between_words(self):
        for title in ("2021 Can-Am Defender HD10", "2021 CAN-AM DEFENDER",
                      "2021 Can Am Defender", "2026 CAN AM defender max"):
            self.assertTrue(self.drops("can am", title), title)

    def test_the_words_still_have_to_be_separate_words(self):
        # 'canam' is a different word, not another spelling of two, so it needs
        # its own term. This is the coverage given up to fix the case below.
        self.assertFalse(self.drops("can am", "2022 Canam defender hd10"))
        self.assertTrue(self.drops("canam", "2022 Canam defender hd10"))

    def test_a_term_does_not_match_inside_a_longer_word(self):
        # The one that mattered: on letters alone, 'fender' hides inside
        # 'Defender' and takes out every Land Rover in the sweep.
        self.assertFalse(self.drops("fender", "1994 Land Rover Defender 110"))
        self.assertTrue(self.drops("fender", "Can-Am Defender Ranch Armor Fender Flares"))

    def test_it_matches_a_word_start_so_plurals_come_along(self):
        self.assertTrue(self.drops("can am", "Can-Ams for sale"))
        self.assertTrue(self.drops("hot wheels", "HOT WHEELS PREMIUM Defender 110"))

    def test_the_term_can_be_written_either_way_round(self):
        for term in ("can am", "Can-Am", "CAN  AM"):
            self.assertTrue(self.drops(term, "2023 Can am defender"), term)

    def test_it_reads_the_card_text_as_well_as_the_title(self):
        r = {"title": "Utility vehicle", "raw_text": "Utility vehicle | Can-Am"}
        self.assertTrue(listings.is_excluded(r, ["can am"]))

    def test_empty_and_punctuation_only_terms_are_ignored(self):
        for term in ("", "   ", ",", "-"):
            self.assertFalse(self.drops(term, "1994 Land Rover Defender 110"), term)
        self.assertFalse(listings.is_excluded({"title": "x", "raw_text": ""}, []))


class YearNumberTest(unittest.TestCase):
    def test_it_reads_the_year_sellers_lead_with(self):
        self.assertEqual(listings.year_number("1994 Land Rover Defender 110"), 1994)
        self.assertEqual(listings.year_number("Land Rover Defender 110 1994"), 1994)

    def test_a_title_without_one_has_no_year(self):
        self.assertIsNone(listings.year_number("Land Rover Defender 110"))
        self.assertIsNone(listings.year_number(""))
        self.assertIsNone(listings.year_number(None))

    def test_numbers_that_are_not_years_are_not_mistaken_for_one(self):
        # The trap this bound exists for: a model number, a part number and a
        # price all look like a year to a bare four-digit match.
        self.assertIsNone(listings.year_number("Defender 110 hardtop, 3500 miles"))
        self.assertIsNone(listings.year_number("Defender part no. 8891"))
        self.assertEqual(listings.year_number("Defender 110, 1200 miles, 1985"), 1985)

    def test_it_agrees_with_the_gallery_about_the_range(self):
        latest = listings.latest_year()
        self.assertEqual(listings.year_number("1900 Defender"), 1900)
        self.assertIsNone(listings.year_number("1899 Defender"))
        self.assertEqual(listings.year_number(f"{latest} Defender"), latest)
        self.assertIsNone(listings.year_number(f"{latest + 1} Defender"))


class YearFilterTest(unittest.TestCase):
    def keep(self, title, **kw):
        return listings.keep_row(row(title), (), None, None, **kw)

    def test_a_year_inside_the_range_is_kept(self):
        self.assertEqual(self.keep("1994 Defender", min_year=1970, max_year=1995),
                         (True, ""))

    def test_a_year_outside_the_range_is_dropped_and_says_why(self):
        self.assertEqual(self.keep("2004 Defender", min_year=1970, max_year=1995),
                         (False, "over max year"))
        self.assertEqual(self.keep("1965 Defender", min_year=1970, max_year=1995),
                         (False, "under min year"))

    def test_one_sided_bounds_only_bind_on_their_own_side(self):
        self.assertEqual(self.keep("2004 Defender", min_year=1970), (True, ""))
        self.assertEqual(self.keep("1965 Defender", max_year=1995), (True, ""))

    def test_undated_listings_are_kept_by_default(self):
        self.assertEqual(self.keep("Defender 110", min_year=1970, max_year=1995),
                         (True, ""))

    def test_undated_listings_can_be_dropped_on_request(self):
        self.assertEqual(
            self.keep("Defender 110", min_year=1970, max_year=1995,
                      include_no_year=False),
            (False, "no year in title"))

    def test_dropping_undated_listings_needs_a_bound_to_act_on(self):
        # Unchecking the box with no year range set would otherwise throw away
        # every listing whose seller simply didn't type a year.
        self.assertEqual(self.keep("Defender 110", include_no_year=False),
                         (True, ""))

    def test_the_earlier_filters_still_come_first(self):
        # A year bound must not promote a listing past the query or exclude
        # tests, which are what the whole sweep rests on.
        r = row("1994 Defender")
        r["matches_query"] = "no"
        self.assertEqual(listings.keep_row(r, (), None, None, 1970, 1995),
                         (False, "query words missing"))


class ScrollProbeTest(unittest.TestCase):
    """The probe decides only whether a scroll was worth doing, so it tests the
    query words and the price and nothing else. Both of the filters it skips
    narrow a feed Facebook is still ordering by relevance, where a run of
    non-passing cards says nothing about what is further down."""

    CARD = {"text": "2004 Land Rover Defender 110\n$40,000\nMedford, OR",
            "outside": False}

    def test_it_ignores_the_year(self):
        # keep_row throws this one out; the probe still counts it, so a run of
        # 2004s can't end a city while the 1980s are further down.
        row = row_from(self.CARD)
        self.assertEqual(listings.keep_row(row, (), None, None, 1970, 1995),
                         (False, "over max year"))
        self.assertTrue(listings.card_may_keep(self.CARD, [["defender"]]))

    def test_it_ignores_the_exclude_terms(self):
        # Can-Am's model really is called the Defender, so this card matches the
        # query and is exactly what an exclude term is for. Dropped from the
        # results, but it still counts towards carrying the scroll on.
        card = {"text": "2021 Can-Am Defender HD10\n$18,000\nMedford, OR",
                "outside": False}
        self.assertEqual(listings.keep_row(row_from(card), ["can am"]),
                         (False, "excluded term"))
        self.assertTrue(listings.card_may_keep(card, [["defender"]]))

    def test_it_still_applies_the_query_words_and_the_price(self):
        self.assertFalse(listings.card_may_keep(self.CARD, [["bronco"]]))
        self.assertFalse(listings.card_may_keep(self.CARD, [["defender"]], 50000))
        self.assertTrue(
            listings.card_may_keep(self.CARD, [["defender"]], None, 50000))

    def test_a_card_matching_any_of_the_queries_carries_the_scroll_on(self):
        # This city is being scrolled for one query, but a card that answers
        # another of the search's queries is a real match and will be kept, so
        # it has to count here too.
        groups = listings.query_groups(["bronco", "defender 110"])
        self.assertTrue(listings.card_may_keep(self.CARD, groups))

    def test_an_unrendered_card_keeps_the_scroll_alive(self):
        self.assertTrue(listings.card_may_keep({"text": "", "outside": False},
                                               [["defender"]]))

    def test_the_out_of_radius_tail_never_counts(self):
        self.assertFalse(listings.card_may_keep({**self.CARD, "outside": True},
                                                [["defender"]]))


if __name__ == "__main__":
    unittest.main()
