import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobsearch import schedule, wizard
from jobsearch.filters import Matcher
from jobsearch.prefs import build_config, location_patterns, phrase_to_regex, summary
from jobsearch.sources import Job

ROOT = Path(__file__).resolve().parent.parent


def job(title, loc="Chicago, IL", desc="", remote=False):
    return Job("test", "1", "Acme", title, [loc], "https://x", description=desc, remote=remote)


def cfg(**over):
    raw = {"locations": ["Chicago"], "roles": {"Data": ["data analyst", "data scien*"]},
           "experience": "entry"}
    raw.update(over)
    return build_config(raw)


class Phrases(unittest.TestCase):
    def test_whole_words_and_case(self):
        p = re.compile(phrase_to_regex("data analyst"), re.I)
        self.assertTrue(p.search("Senior Data  Analyst, Growth"))
        self.assertFalse(p.search("Big Data Analysts"))

    def test_wildcard(self):
        p = re.compile(phrase_to_regex("product manag*"), re.I)
        self.assertTrue(p.search("Product Management Associate"))
        self.assertTrue(p.search("Product Manager"))

    def test_raw_regex(self):
        self.assertEqual(phrase_to_regex(r"re:\bSWE\b"), r"\bSWE\b")

    def test_location_aliases(self):
        pats = [re.compile(p, re.I) for p in location_patterns(["New York"])]
        hit = lambda s: any(p.search(s) for p in pats)
        self.assertTrue(hit("NYC"))
        self.assertTrue(hit("Brooklyn, NY"))
        self.assertFalse(hit("New York State Dept"))

    def test_symbols_in_city(self):
        pats = [re.compile(p, re.I) for p in location_patterns(["St. Louis"])]
        self.assertTrue(any(p.search("St. Louis, MO") for p in pats))


class Config(unittest.TestCase):
    def test_shipped_config_builds(self):
        c = build_config(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))
        self.assertTrue(c["locations"]["include_remote"])
        self.assertIn("Data", c["roles"])

    def test_remote_in_locations(self):
        c = cfg(locations=["Remote"])
        self.assertTrue(c["locations"]["include_remote"])
        self.assertEqual(c["locations"]["include"], [])

    def test_requires_roles_and_locations(self):
        with self.assertRaises(ValueError):
            cfg(roles={})
        with self.assertRaises(ValueError):
            cfg(locations=[])

    def test_advanced_override_and_extra_companies(self):
        c = cfg(advanced={"max_years_experience": 3},
                extra_companies={"greenhouse": ["AcmeCo"]})
        self.assertEqual(c["max_years_experience"], 3)
        self.assertIn("acmeco", c["sources"]["greenhouse"])
        self.assertIn("stripe", c["sources"]["greenhouse"])

    def test_old_regex_format_passes_through(self):
        old = {"locations": {"include": [r"\bX\b"]}, "roles": {"A": ["a"]}}
        self.assertEqual(build_config(old)["locations"], old["locations"])

    def test_summary(self):
        self.assertEqual(summary({"experience": "mid", "locations": ["Austin", "Remote"]}),
                         "Mid-level roles in Austin and remote")


class Matching(unittest.TestCase):
    def test_entry(self):
        m = Matcher(cfg())
        self.assertTrue(m.prefilter(job("Data Analyst")))
        self.assertFalse(m.prefilter(job("Senior Data Analyst")))
        self.assertFalse(m.prefilter(job("Data Analyst", loc="Denver, CO")))
        self.assertTrue(m.seniority_ok(job("Data Analyst")))            # entry word
        self.assertFalse(m.seniority_ok(job("Data Scientist", desc="5+ years of experience")))

    def test_mid(self):
        m = Matcher(cfg(experience="mid"))
        self.assertTrue(m.prefilter(job("Senior Data Scientist")))
        self.assertFalse(m.prefilter(job("Junior Data Analyst")))
        self.assertFalse(m.prefilter(job("Principal Data Scientist")))
        self.assertTrue(m.seniority_ok(job("Data Scientist", desc="3+ years of experience")))
        self.assertTrue(m.seniority_ok(job("Data Scientist")))          # unstated is fine

    def test_exclude_words(self):
        m = Matcher(cfg(exclude_words=["marketing"]))
        self.assertFalse(m.prefilter(job("Marketing Data Analyst")))

    def test_remote(self):
        m = Matcher(cfg(locations=["Remote"]))
        self.assertTrue(m.prefilter(job("Data Analyst", loc="Remote - US")))
        self.assertFalse(m.prefilter(job("Data Analyst", loc="Chicago, IL")))


class Schedule(unittest.TestCase):
    def test_cron_lines(self):
        self.assertEqual(schedule.cron_lines("America/New_York", 7), ["0 12 * * *", "0 11 * * *"])
        self.assertEqual(schedule.cron_lines("America/Phoenix", 7), ["0 14 * * *"])
        self.assertEqual(schedule.cron_lines("Asia/Kolkata", 9, 0), ["30 3 * * *"])
        with self.assertRaises(ValueError):
            schedule.cron_lines("Mars/Olympus", 7)

    def test_gate(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/New_York")
        except Exception:
            self.skipTest("no time-zone database on this machine (fine on GitHub Actions)")
        sched = {"timezone": "America/New_York", "hour": 7, "minute": 0}
        jan = datetime(2027, 1, 10, 12, 14, tzinfo=timezone.utc)       # EST
        jul = datetime(2027, 7, 10, 11, 20, tzinfo=timezone.utc)       # EDT
        self.assertTrue(schedule.should_run("schedule", "0 12 * * *", sched, jan))
        self.assertFalse(schedule.should_run("schedule", "0 11 * * *", sched, jan))
        self.assertTrue(schedule.should_run("schedule", "0 11 * * *", sched, jul))
        self.assertTrue(schedule.should_run("workflow_dispatch", "", sched, jan))

    def test_write_workflow_cron(self):
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d) / "wf.yml"
            wf.write_text((ROOT / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8"),
                          encoding="utf-8")
            schedule.write_workflow_cron(["5 9 * * *"], wf)
            text = wf.read_text(encoding="utf-8")
            self.assertIn('- cron: "5 9 * * *"', text)
            self.assertNotIn('"0 12 * * *"', text)


class Wizard(unittest.TestCase):
    def test_run(self):
        answers = iter(["Austin, Remote", "Analytics", "data analyst, business analyst*", "",
                        "2", "sales", "America/Chicago", "8", "n"])
        with tempfile.TemporaryDirectory() as d:
            conf, wf = Path(d) / "config.json", Path(d) / "wf.yml"
            wf.write_text((ROOT / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8"),
                          encoding="utf-8")
            out = wizard.run(ask=lambda q: next(answers), say=lambda *a: None, config=conf, workflow=wf)
            saved = json.loads(conf.read_text(encoding="utf-8"))
            self.assertEqual(saved, out)
            self.assertEqual(saved["locations"], ["Austin", "Remote"])
            self.assertEqual(saved["roles"], {"Analytics": ["data analyst", "business analyst*"]})
            self.assertEqual(saved["experience"], "mid")
            self.assertEqual(saved["schedule"]["hour"], 8)
            self.assertFalse(saved["track_applications"])
            self.assertIn('- cron: "0 14 * * *"', wf.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
