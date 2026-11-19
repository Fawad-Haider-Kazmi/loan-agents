import os
import re
import unittest

from tests.helpers import make_system
from loan_system.state import ALLOWED_TRANSITIONS

DOC = os.path.join(os.path.dirname(__file__), "..", "docs", "ARCHITECTURE.md")


class DocumentationMatchesCodeTests(unittest.TestCase):
    def setUp(self):
        with open(DOC, encoding="utf-8") as fh:
            self.md = fh.read()

    def test_state_diagram_matches_transition_table(self):
        block = re.search(r"stateDiagram-v2\n(.*?)```", self.md, re.S).group(1)
        documented = set()
        for src, dst in re.findall(r"^\s*(\w+|\[\*\]) --> (\w+|\[\*\])", block, re.M):
            if "[*]" not in (src, dst):
                documented.add((src, dst))
        in_code = {(a.value, b.value) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
        self.assertEqual(documented, in_code)

    def test_diagram_agent_names_exist_as_classes(self):
        system, _, _ = make_system()
        class_names = {type(a).__name__ for a in system.workflow._agents.values()}
        for name in class_names:
            self.assertIn(name, self.md)
        self.assertEqual(len(class_names), 8)

    def test_all_required_diagrams_present(self):
        titles = re.findall(r"^## \d+\. (.+)$", self.md, re.M)
        for needed in ("Existing process", "multi-agent architecture", "Agent communication flow",
                       "Data flow diagram", "State diagram", "System architecture", "Human-in-the-loop"):
            self.assertTrue(any(needed.lower() in t.lower() for t in titles), needed)
        self.assertEqual(self.md.count("```mermaid"), 8)


if __name__ == "__main__":
    unittest.main()
