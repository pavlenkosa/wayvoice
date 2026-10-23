"""WayVoice must not replace authentication prompts for other applications."""

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class PolkitPolicyTests(unittest.TestCase):
    def test_exec_actions_only_target_wayvoice_helpers(self):
        # exec.path matches every caller of that binary, including other apps.
        # Package managers, env and interpreters must retain the system policy.
        # No custom action is needed: pkexec defaults to administrator auth.
        for policy in (REPO / "packaging").rglob("*.policy"):
            for action in ET.parse(policy).getroot().findall("action"):
                for annotation in action.findall("annotate"):
                    if annotation.get("key") == "org.freedesktop.policykit.exec.path":
                        with self.subTest(policy=policy.name, action=action.get("id")):
                            self.assertTrue(
                                Path(annotation.text or "").name.startswith("wayvoice-"),
                                "Custom authentication must target a dedicated WayVoice helper",
                            )


if __name__ == "__main__":
    unittest.main()
