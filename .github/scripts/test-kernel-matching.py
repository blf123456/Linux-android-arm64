"""Exercise the installer's real selection code without loading a module."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

PACKER = Path(__file__).resolve().parents[2] / "packer.sh"
LOGIC = PACKER.read_text(encoding="utf-8").split("KERNEL_VER=$(uname -r)", 1)[1].split("\nLOGIC_END", 1)[0]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
HARNESS = r'''
uname() { printf '%s\n' "$TEST_RELEASE"; }
cat() { [ "$1" = /proc/version ] || exit 99; printf '%s\n' "$TEST_PROC"; }
load_driver_logic() { printf 'SELECTED=%s\n' "$2"; }
KERNEL_VER=$(uname -r)
''' + LOGIC


class KernelMatchingTest(unittest.TestCase):
    def select(self, release, proc=""):
        result = subprocess.run(
            [BASH, "-s"], input=HARNESS, text=True, encoding="utf-8",
            capture_output=True, env={**os.environ, "TEST_RELEASE": release, "TEST_PROC": proc},
            timeout=10,
        )
        self.assertEqual(result.stderr, "", result.stderr)
        return result.returncode, result.stdout

    def test_all_supported_pairs_ignore_patch_and_vendor(self):
        pairs = [("5.10", 12), ("5.10", 13), ("5.15", 13), ("6.1", 14),
                 ("6.6", 15), ("6.12", 16), ("6.18", 17)]
        for linux, android in pairs:
            for patch in [0, 209, 999]:
                with self.subTest(linux=linux, android=android, patch=patch):
                    code, output = self.select(f"{linux}.{patch}-android{android}-9-o-vendor+")
                    self.assertEqual(code, 0, output)
                    self.assertIn(f"SELECTED={linux}-Android{android}\n", output)

    def test_branch_delimiters_and_case(self):
        for release in ["5.10.209-Android12-9", "5.10.209_android12_vendor", "5.10.209+android12"]:
            with self.subTest(release=release):
                self.assertEqual(self.select(release), (0, "SELECTED=5.10-Android12\n"))

    def test_proc_fallback_is_kernel_data(self):
        self.assertEqual(self.select("5.10.209-vendor", "Linux version 5.10.209 (android12)"),
                         (0, "SELECTED=5.10-Android12\n"))

    def test_uname_branch_takes_precedence(self):
        self.assertEqual(self.select("5.10.209-android12", "build host android13"),
                         (0, "SELECTED=5.10-Android12\n"))

    def test_reject_unknown_ambiguous_or_missing_branch(self):
        for release, proc in [
            ("5.15.100-android14", ""), ("5.10.209-vendor", ""),
            ("5.10.209-android12-android13", ""),
            ("5.10.209-vendor", "android12 android13"),
            ("5.10.209-notandroid12", ""), ("5.10.209-android12custom", ""),
            ("5.4.200-android12", ""), ("invalid-android12", ""),
        ]:
            with self.subTest(release=release, proc=proc):
                code, output = self.select(release, proc)
                self.assertNotEqual(code, 0, output)
                self.assertNotIn("SELECTED=", output)


if __name__ == "__main__":
    unittest.main()
