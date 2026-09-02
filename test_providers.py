#!/usr/bin/env python3
"""Matcher and JustWatch-merge tests for YouTube's free tier.

No network in the default suite: JustWatch and TMDB are fixtures. A live
JustWatch check for Prisoners lives under test_justwatch_prisoners_live
and is skipped unless RUN_LIVE=1, so a 4am job never depends on it.
"""

import os
import tempfile
import unittest
from pathlib import Path

import common
import render
import sync_providers


class FreeTierForTests(unittest.TestCase):
    def test_youtube_free_canonical_name(self):
        self.assertEqual(common.free_tier_for("YouTube Free"), "YouTube (free)")

    def test_bare_youtube_name_is_not_enough(self):
        # Id 192 is recognized at store time, not by this name. A bare
        # "youtube" prefix would also catch YouTube TV / Premium / Music.
        self.assertIsNone(common.free_tier_for("YouTube"))
        self.assertIsNone(common.free_tier_for("YouTube Movies"))

    def test_rejects_youtube_tv(self):
        self.assertIsNone(common.free_tier_for("YouTube TV"))

    def test_rejects_youtube_premium(self):
        self.assertIsNone(common.free_tier_for("YouTube Premium"))

    def test_rejects_channel_and_store(self):
        self.assertIsNone(common.free_tier_for("YouTube Free Channel"))
        self.assertIsNone(common.free_tier_for("YouTube Store"))

    def test_rejects_other_free_services(self):
        self.assertIsNone(common.free_tier_for("Tubi"))
        self.assertIsNone(common.free_tier_for("Pluto TV"))
        self.assertIsNone(common.free_tier_for("The Roku Channel"))

    def test_empty_and_none(self):
        self.assertIsNone(common.free_tier_for(""))
        self.assertIsNone(common.free_tier_for(None))
        self.assertIsNone(common.free_tier_for("   "))


class PairsFromRegionTests(unittest.TestCase):
    def test_keeps_youtube_free_by_name(self):
        pairs = sync_providers.pairs_from_region({
            "ads": [{"provider_name": "YouTube Free", "provider_id": 235}],
        })
        self.assertIn(("ads", "YouTube Free"), pairs)

    def test_keeps_bare_youtube_in_ads_as_canonical_name(self):
        pairs = sync_providers.pairs_from_region({
            "ads": [{"provider_name": "YouTube", "provider_id": 192}],
        })
        self.assertIn(("ads", common.YOUTUBE_FREE_PROVIDER), pairs)

    def test_keeps_youtube_free_by_id_alone(self):
        pairs = sync_providers.pairs_from_region({
            "ads": [{"provider_name": "YT Free (renamed)", "provider_id": 235}],
        })
        self.assertIn(("ads", common.YOUTUBE_FREE_PROVIDER), pairs)

    def test_drops_tubi(self):
        pairs = sync_providers.pairs_from_region({
            "ads": [{"provider_name": "Tubi", "provider_id": 73}],
            "free": [{"provider_name": "Pluto TV", "provider_id": 300}],
        })
        self.assertEqual(pairs, set())

    def test_does_not_promote_youtube_rent_to_free(self):
        pairs = sync_providers.pairs_from_region({
            "rent": [{"provider_name": "YouTube", "provider_id": 192}],
            "buy": [{"provider_name": "YouTube", "provider_id": 192}],
        })
        self.assertEqual(
            pairs,
            {("rent", "YouTube"), ("buy", "YouTube")},
        )
        self.assertFalse(any(k in common.FREE_KINDS for k, _ in pairs))

    def test_keeps_flatrate_and_ignores_junk(self):
        pairs = sync_providers.pairs_from_region({
            "flatrate": [{"provider_name": "Netflix", "provider_id": 8}, "not-a-dict"],
            "ads": [None, {"provider_name": "", "provider_id": 235}],
        })
        self.assertIn(("flatrate", "Netflix"), pairs)
        # id 235 with an empty name still stores the canonical name
        self.assertIn(("ads", common.YOUTUBE_FREE_PROVIDER), pairs)


class AddYoutubeFreeTests(unittest.TestCase):
    def test_adds_canonical_ads_row(self):
        pairs = {("flatrate", "Netflix")}
        sync_providers.add_youtube_free(pairs, True)
        self.assertIn((common.FREE_KINDS[0], common.YOUTUBE_FREE_PROVIDER), pairs)

    def test_noop_when_absent(self):
        pairs = {("flatrate", "Netflix")}
        sync_providers.add_youtube_free(pairs, False)
        self.assertEqual(pairs, {("flatrate", "Netflix")})

    def test_idempotent_with_tmdb_row_already_present(self):
        pairs = {("ads", "YouTube Free")}
        sync_providers.add_youtube_free(pairs, True)
        self.assertEqual(pairs, {("ads", "YouTube Free")})


class RenderSnapshotTests(unittest.TestCase):
    def test_prisoners_youtube_free_and_owned_both_show(self):
        """The page tag list is YouTube (free) plus Owned · Amazon Prime.

        This is the Prisoners case: owned on Prime, free on YouTube, and the
        YouTube row has to survive snapshot() then merge_owned() so the card
        doesn't keep looking like Prime-only.
        """
        path = Path(tempfile.mkdtemp()) / "movies.db"
        conn = common.connect(path)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO movies (tmdb_id, title, year, runtime, poster_path, "
            "overview, director, genres, vote_average, vote_count, top_cast, "
            "trailer_key, status, release_date, certification) "
            "VALUES (146233, 'Prisoners', 2013, 153, '', '', '', "
            "'Drama, Thriller, Crime', 8.1, 1, '', '', 'Released', "
            "'2013-09-20', 'R')"
        )
        conn.execute("INSERT INTO favorites (tmdb_id) VALUES (146233)")
        conn.execute("INSERT INTO owned (tmdb_id, store) VALUES (146233, 'Amazon Prime')")
        conn.execute("INSERT INTO poll_log (tmdb_id, polled_on) VALUES (146233, '2026-09-02')")
        conn.execute(
            "INSERT INTO availability (tmdb_id, provider, kind, seen_on) "
            "VALUES (146233, 'YouTube Free', 'ads', '2026-09-02')"
        )
        conn.commit()

        today = render.snapshot(conn, "2026-09-02", "favorites")
        self.assertEqual(today[146233], ["YouTube (free)"])
        owned = render.owned_snapshot(conn)
        merged = render.merge_owned(today, owned, ids={146233})
        self.assertEqual(
            merged[146233],
            ["YouTube (free)", "Owned · Amazon Prime"],
        )


@unittest.skipUnless(os.environ.get("RUN_LIVE") == "1", "set RUN_LIVE=1")
class JustWatchPrisonersLive(unittest.TestCase):
    def test_prisoners_is_youtube_free(self):
        lookup = sync_providers.YouTubeFreeLookup()
        self.assertTrue(
            lookup.has(146233, "Prisoners", 2013),
            "JustWatch should list Prisoners (2013, tmdb 146233) on YouTube Free",
        )


if __name__ == "__main__":
    unittest.main()
