"""Templates keep HTML escaping on: no `|safe`, `autoescape false` or Markup in templates or web adapters."""

from pathlib import Path
import re
import unittest

WEB = Path(__file__).parents[1] / "src/repairlib/adapters/web"
SWITCHES = re.compile(r"\|\s*safe\b|autoescape\s+(?:false|off)|\bMarkup\(")


class TemplateSafetyTests(unittest.TestCase):
    def test_nothing_switches_escaping_off(self):
        found = [f"{path.name}:{number}" for path in sorted([*WEB.glob("templates/*.html"), *WEB.glob("*.py")])
                 for number, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1)
                 if SWITCHES.search(line)]
        self.assertEqual(found, [])

    def test_the_pattern_matches_what_it_forbids(self):
        for line in ("{{ x|safe }}", "{{ x | safe }}", "{% autoescape false %}", "Markup(value)"):
            self.assertTrue(SWITCHES.search(line), line)


if __name__ == "__main__":
    unittest.main()
