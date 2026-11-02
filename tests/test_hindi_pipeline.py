import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402


class HindiPipelineTests(unittest.TestCase):
    def test_english_candidate_gets_hindi_copy_with_same_sources(self):
        candidate = {
            "lang": "en", "title": "Government announces a new public transport policy",
            "summary": "The government announced a new public transport policy today. " * 5,
            "keyword": "public transport", "category": "india",
            "published": engine.datetime(2026, 11, 2, 5, 0, tzinfo=engine.timezone.utc),
            "sources": [{"title": "Original English headline", "url": "https://news.example/story",
                         "publisher": "Example News"}],
            "feed": "English feed", "weight": 0, "image_url": "",
        }
        translations = {
            candidate["title"]: "सरकार ने नई सार्वजनिक परिवहन नीति की घोषणा की",
            candidate["summary"]: "सरकार ने आज नई सार्वजनिक परिवहन नीति की घोषणा की। " * 5,
            candidate["keyword"]: "सार्वजनिक परिवहन",
        }
        with mock.patch.object(engine, "translate_to_hindi", side_effect=lambda value: translations.get(value, value)):
            result = engine.translate_candidate_to_hindi(candidate, {"translate_english_to_hindi": True,
                                                                       "min_title_chars": 12})
        self.assertIsNotNone(result)
        self.assertEqual(result["lang"], "hi")
        self.assertEqual(result["title"], translations[candidate["title"]])
        self.assertEqual(result["sources"], candidate["sources"])
        self.assertEqual(result["sources"][0]["url"], candidate["sources"][0]["url"])
        self.assertEqual(candidate["lang"], "en")

    def test_english_candidate_is_not_labelled_hindi_if_translation_fails(self):
        candidate = {
            "lang": "en", "title": "Government announces a new public transport policy",
            "summary": "The government announced a new public transport policy today. " * 5,
            "sources": [], "keyword": "", "category": "india", "feed": "English",
        }
        with mock.patch.object(engine, "translate_to_hindi", side_effect=lambda value: value):
            result = engine.translate_candidate_to_hindi(candidate, {"translate_english_to_hindi": True,
                                                                       "min_title_chars": 12})
        self.assertIsNone(result)

    def test_candidate_order_alternates_languages(self):
        candidates = [
            {"lang": "en", "score": 100}, {"lang": "en", "score": 90},
            {"lang": "hi", "score": 80}, {"lang": "hi", "score": 70},
            {"lang": "en", "score": 60},
        ]
        ordered = engine.balanced_candidates(candidates)
        self.assertEqual([c["lang"] for c in ordered], ["hi", "en", "hi", "en", "en"])
        self.assertEqual([c["score"] for c in ordered], [80, 100, 70, 90, 60])


if __name__ == "__main__":
    unittest.main()
