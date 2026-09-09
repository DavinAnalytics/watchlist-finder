#!/usr/bin/env python3
"""URL parsing for sync.pages_url_from_remote — no network."""

import unittest

import sync


class PagesUrlTests(unittest.TestCase):
    def test_https(self):
        self.assertEqual(
            sync.pages_url_from_remote(
                "https://github.com/DavinAnalytics/watchlist-finder"
            ),
            "https://davinanalytics.github.io/watchlist-finder/",
        )

    def test_https_git_suffix(self):
        self.assertEqual(
            sync.pages_url_from_remote(
                "https://github.com/DavinAnalytics/watchlist-finder.git"
            ),
            "https://davinanalytics.github.io/watchlist-finder/",
        )

    def test_ssh(self):
        self.assertEqual(
            sync.pages_url_from_remote(
                "git@github.com:DavinAnalytics/watchlist-finder.git"
            ),
            "https://davinanalytics.github.io/watchlist-finder/",
        )

    def test_actions_token_remote(self):
        self.assertEqual(
            sync.pages_url_from_remote(
                "https://x-access-token:ghs_example@github.com/"
                "DavinAnalytics/watchlist-finder"
            ),
            "https://davinanalytics.github.io/watchlist-finder/",
        )

    def test_user_pages_repo(self):
        self.assertEqual(
            sync.pages_url_from_remote("https://github.com/Ada/ada.github.io"),
            "https://ada.github.io/",
        )

    def test_rejects_non_github(self):
        self.assertIsNone(sync.pages_url_from_remote("https://gitlab.com/x/y"))
        self.assertIsNone(sync.pages_url_from_remote(""))
        self.assertIsNone(sync.pages_url_from_remote(None))


if __name__ == "__main__":
    unittest.main()
