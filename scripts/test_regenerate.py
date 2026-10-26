"""Offline regression tests using real uv resolution and tiny wheel metadata.

The local wheel directory supplies release metadata and resolver candidates.
No package code is installed or executed. Only the entry-point pair starts a
child Python; other decisions run in this process with the same real resolver.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import regenerate
from packaging.utils import parse_wheel_filename


def wheel(directory: Path, name: str, version: str, requires: str = "") -> Path:
    stem = f"{name.replace('-', '_')}-{version}"
    path = directory / f"{stem}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
    if requires:
        metadata += f"Requires-Dist: {requires}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{stem}.dist-info/METADATA", metadata)
        archive.writestr(
            f"{stem}.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n"
        )
        archive.writestr(f"{stem}.dist-info/RECORD", "")
    return path


@contextlib.contextmanager
def registry(directory: Path):
    """Substitute artifact discovery, preserving the production resolver argv."""
    original_run = regenerate.run

    def local_run(cmd, stdin=None, capture=False):
        return original_run(
            [
                *cmd,
                "--no-config",
                "--no-index",
                "--offline",
                "--find-links",
                str(directory),
            ],
            stdin=stdin,
            capture=capture,
        )

    def files(name, version):
        result = []
        for path in directory.glob("*.whl"):
            package, release, _, _ = parse_wheel_filename(path.name)
            if package == name and str(release) == version:
                result.append(
                    {
                        "filename": path.name,
                        "url": path.as_uri(),
                        "digests": {
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                        },
                    }
                )
        return result

    with (
        patch.object(regenerate, "run", side_effect=local_run),
        patch.object(regenerate, "pypi_release_files", side_effect=files),
        patch.object(regenerate, "pypi_latest_version", return_value="1.0"),
        patch.object(regenerate, "FORMULA", directory / "formula.rb"),
    ):
        yield


class FormulaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.formula = self.directory / "formula.rb"
        self.enterContext(registry(self.directory))
        wheel(self.directory, "analog-sdk", "1.0", "leaf>=1,<3")
        wheel(self.directory, "leaf", "1.0")
        self.original = regenerate.render("1.0")
        self.formula.write_text(self.original)
        # A newer compatible release exists after the reviewed formula is made.
        wheel(self.directory, "leaf", "2.0")
        wheel(self.directory, "leaf", "3.0")

    def invoke(self, flag):
        with patch.object(sys, "argv", ["regenerate.py", flag]):
            return regenerate.main()

    def test_entrypoint_accepts_reviewed_pins_despite_newer_release(self):
        result = subprocess.run(
            [sys.executable, __file__, "--entrypoint", str(self.directory), "--verify"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("formula integrity verified", result.stdout)
        self.assertEqual(self.formula.read_text(), self.original)

    def test_entrypoint_rejects_incompatible_pin(self):
        # All URL/hash/template bytes are generator-authored, but SDK requires <3.
        with patch.object(
            regenerate,
            "resolve_tree",
            return_value={"analog-sdk": "1.0", "leaf": "3.0"},
        ):
            self.formula.write_text(regenerate.render("1.0"))
        before = self.formula.read_text()
        result = subprocess.run(
            [sys.executable, __file__, "--entrypoint", str(self.directory), "--verify"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("formula integrity failed", result.stderr)
        self.assertIn("No solution found", result.stderr)
        self.assertEqual(self.formula.read_text(), before)

    def test_currency_check_still_detects_newer_resource(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.invoke("--check"), 1)
        self.assertIn("resources drifted", output.getvalue())
        self.assertEqual(self.formula.read_text(), self.original)

    def test_latest_update_selects_newer_compatible_resource(self):
        self.assertEqual(self.invoke("--latest"), 0)
        self.assertEqual(
            regenerate.formula_pins(self.formula.read_text())["leaf"], "2.0"
        )
        self.assertEqual(self.invoke("--verify"), 0)
        self.assertEqual(self.invoke("--check"), 0)

    def test_check_detects_new_sdk(self):
        with patch.object(regenerate, "pypi_latest_version", return_value="2.0"):
            self.assertEqual(self.invoke("--check"), 1)

    def test_removed_dependency_is_not_silently_resolved(self):
        self.formula.write_text(
            self.original.replace(regenerate.resource_block("leaf", "1.0"), "")
        )
        with self.assertRaisesRegex(ValueError, "not exactly the SDK dependency tree"):
            regenerate.verify_formula()

    def test_extra_dependency_is_not_promoted_to_a_root(self):
        wheel(self.directory, "extra", "1.0")
        extra = regenerate.resource_block("extra", "1.0")
        self.formula.write_text(
            self.original.replace("  def install", extra + "\n  def install")
        )
        with self.assertRaisesRegex(ValueError, "not exactly the SDK dependency tree"):
            regenerate.verify_formula()

    def test_hash_url_and_template_edits_fail(self):
        for before, after in (
            ('  sha256 "', '  sha256 "0'),
            (self.directory.as_uri(), "https://example.invalid"),
            ('  license "MIT"', '  license "Apache-2.0"'),
            ('  resource "leaf"', '  resource "renamed"'),
        ):
            with self.subTest(before=before):
                self.formula.write_text(self.original.replace(before, after, 1))
                with self.assertRaisesRegex(
                    ValueError, "generated URLs, hashes or structure"
                ):
                    regenerate.verify_formula()

    def test_duplicate_resource_fails_full_render_comparison(self):
        self.formula.write_text(
            self.original + regenerate.resource_block("leaf", "1.0")
        )
        with self.assertRaisesRegex(ValueError, "generated URLs, hashes or structure"):
            regenerate.verify_formula()

    def test_conflicting_arch_versions_fail(self):
        with self.assertRaisesRegex(ValueError, "conflicting wheel versions"):
            regenerate.formula_pins(
                self.original + regenerate.resource_block("leaf", "2.0")
            )

    def test_missing_sdk_and_non_wheel_urls_fail(self):
        for text in (
            'url "https://example.invalid/file.tar.gz"',
            'resource "empty" do\nend',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                regenerate.formula_pins(text)

    def test_architecture_trees_must_agree(self):
        with patch.object(
            regenerate, "run", side_effect=["leaf==1.0\n", "leaf==2.0\n"]
        ):
            with self.assertRaisesRegex(SystemExit, "trees disagree"):
                regenerate.resolve_tree("1.0")

    def test_architecture_wheel_hashes_are_validated(self):
        files = [
            {
                "filename": "leaf-1.0-cp313-cp313-macosx_11_0_arm64.whl",
                "url": "arm",
                "digests": {"sha256": "a"},
            },
            {
                "filename": "leaf-1.0-cp313-cp313-macosx_10_13_x86_64.whl",
                "url": "intel",
                "digests": {"sha256": "b"},
            },
        ]
        with patch.object(regenerate, "pypi_release_files", return_value=files):
            block = regenerate.resource_block("leaf", "1.0")
        self.assertIn('on_arm do\n      url "arm"', block)
        self.assertIn('on_intel do\n      url "intel"', block)
        self.assertIn('sha256 "a"', block)
        self.assertIn('sha256 "b"', block)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--entrypoint":
        directory, flag = Path(sys.argv[2]), sys.argv[3]
        with registry(directory), patch.object(sys, "argv", ["regenerate.py", flag]):
            sys.exit(regenerate.main())
    unittest.main()
