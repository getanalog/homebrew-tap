#!/usr/bin/env python3
"""Regenerate Formula/analog.rb for a given analog-sdk version.

The formula is a venv plus hash-pinned wheels, and the pin set is
machine-derived. Never edit the pins by hand; run this instead (the
bump workflow and the poller both do).

    uv run --with packaging python scripts/regenerate.py --version 0.10.0
    uv run --with packaging python scripts/regenerate.py --latest
    uv run --with packaging python scripts/regenerate.py --check

Needs uv on PATH and network access to pypi.org. Hashes come from the
PyPI JSON API, same data pip verifies against.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename

PACKAGE = "analog-sdk"
PYTHON_MINOR = (3, 13)  # must match the formula's depends_on "python@3.13"
FORMULA = Path(__file__).resolve().parent.parent / "Formula" / "analog.rb"

ARCHS = ("arm64", "x86_64")


def acceptable_tags(arch: str) -> list:
    """Wheel tags installable on macOS `arch` under our CPython, best first."""
    platforms = list(mac_platforms((26, 0), arch))
    tags = list(cpython_tags(python_version=PYTHON_MINOR, platforms=platforms))
    tags += list(compatible_tags(python_version=PYTHON_MINOR, platforms=platforms))
    return tags


def resolve_tree(version: str) -> dict[str, str]:
    """name -> version for the macOS runtime tree of analog-sdk==version.

    Resolved with an explicit --python-platform so the tree is the
    formula's target platform, not the machine this script happens to
    run on. (Resolving on a Linux runner otherwise pulls keyring's
    Linux-only deps like cryptography into a macOS formula.) Both
    formula arches must agree on the package set.
    """
    trees: dict[str, dict[str, str]] = {}
    for platform in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
        compiled = run(
            [
                "uv", "pip", "compile", "-",
                "--python-platform", platform,
                "--python-version", f"{PYTHON_MINOR[0]}.{PYTHON_MINOR[1]}",
                "--no-header", "--no-annotate", "--quiet",
            ],
            stdin=f"{PACKAGE}=={version}\n",
            capture=True,
        )
        tree: dict[str, str] = {}
        for line in compiled.splitlines():
            if "==" not in line:
                continue
            name, _, ver = line.partition("==")
            tree[canonicalize_name(name.strip())] = ver.strip()
        trees[platform] = tree
    if trees["aarch64-apple-darwin"] != trees["x86_64-apple-darwin"]:
        raise SystemExit(
            "ERROR: the arm64 and x86_64 macOS trees disagree; the shared "
            "resource list can't represent that. Diff the two uv resolutions."
        )
    return trees["aarch64-apple-darwin"]


def run(cmd: list[str], stdin: str | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        cmd,
        input=stdin,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout if capture else ""


def pypi_release_files(name: str, version: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)["urls"]


def pypi_latest_version(name: str) -> str:
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)["info"]["version"]


def pick_wheel(files: list[dict], arch: str) -> dict | None:
    """The best installable wheel for macOS `arch`, by tag preference."""
    ranked = acceptable_tags(arch)
    best: tuple[int, dict] | None = None
    for f in files:
        if not f["filename"].endswith(".whl") or f.get("yanked"):
            continue
        _, _, _, tags = parse_wheel_filename(f["filename"])
        ranks = [ranked.index(t) for t in tags if t in ranked]
        if not ranks:
            continue
        rank = min(ranks)
        if best is None or rank < best[0]:
            best = (rank, f)
    return best[1] if best else None


def resource_block(name: str, version: str) -> str:
    files = pypi_release_files(name, version)
    per_arch = {}
    for arch in ARCHS:
        wheel = pick_wheel(files, arch)
        if wheel is None:
            raise SystemExit(
                f"ERROR: no installable macOS/{arch} wheel for {name}=={version} "
                "— the pragmatic wheel formula can't ship this version."
            )
        per_arch[arch] = wheel

    if per_arch["arm64"]["filename"] == per_arch["x86_64"]["filename"]:
        w = per_arch["arm64"]
        body = (
            f'    url "{w["url"]}", using: :nounzip\n'
            f'    sha256 "{w["digests"]["sha256"]}"\n'
        )
    else:
        arm, intel = per_arch["arm64"], per_arch["x86_64"]
        body = (
            "    on_arm do\n"
            f'      url "{arm["url"]}", using: :nounzip\n'
            f'      sha256 "{arm["digests"]["sha256"]}"\n'
            "    end\n"
            "    on_intel do\n"
            f'      url "{intel["url"]}", using: :nounzip\n'
            f'      sha256 "{intel["digests"]["sha256"]}"\n'
            "    end\n"
        )
    return f'  resource "{name}" do\n{body}  end\n'


def render(version: str) -> str:
    tree = resolve_tree(version)
    sdk_version = tree.pop(PACKAGE)
    assert sdk_version == version, f"resolved {sdk_version}, expected {version}"

    sdk_files = pypi_release_files(PACKAGE, version)
    sdk_wheel = pick_wheel(sdk_files, "arm64")
    if sdk_wheel is None:
        raise SystemExit(f"ERROR: no wheel for {PACKAGE}=={version}")

    resources = "\n".join(
        resource_block(name, ver) for name, ver in sorted(tree.items())
    )

    return f'''# Generated by scripts/regenerate.py. Don't edit the pins by hand;
# bump via the workflow (or run the script) instead.
class Analog < Formula
  include Language::Python::Virtualenv

  desc "Turn webpages into structured data"
  homepage "https://getanalog.io"
  url "{sdk_wheel["url"]}", using: :nounzip
  sha256 "{sdk_wheel["digests"]["sha256"]}"
  license "MIT"

  # brew audit wants these for the pyyaml/lxml resources (it assumes
  # sdist builds). The wheels bundle the libs, so these cost nothing.
  depends_on "libyaml"
  depends_on :macos
  depends_on "python@{PYTHON_MINOR[0]}.{PYTHON_MINOR[1]}"

  uses_from_macos "libxml2"
  uses_from_macos "libxslt"

{resources}
  def install
    virtualenv_create(libexec, "python{PYTHON_MINOR[0]}.{PYTHON_MINOR[1]}")
    # Wheels are already verified by brew's download step. Keep pip
    # off the network and out of the resolver.
    resources.each do |r|
      r.stage do
        wheel = Dir["*.whl"].first
        system libexec/"bin/python", "-m", "pip", "install",
               "--no-deps", "--no-index", "--no-cache-dir", "--quiet", wheel
      end
    end
    wheel = Dir[buildpath/"*.whl"].first
    system libexec/"bin/python", "-m", "pip", "install",
           "--no-deps", "--no-index", "--no-cache-dir", "--quiet", wheel
    # Tell the CLI which channel installed it, so its upgrade hints
    # say `brew upgrade` here instead of the pip command.
    (libexec/"lib/python{PYTHON_MINOR[0]}.{PYTHON_MINOR[1]}/site-packages/analog/INSTALL_CHANNEL").write "brew\n"
    bin.install_symlink libexec/"bin/analog"
  end

  def caveats
    <<~EOS
      JS-heavy pages need the bundled browser (one-time ~150 MB download):
        analog browser install

      Connect this machine to your Analog account:
        analog login
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/analog --version")
  end
end
'''


def formula_pinned_version() -> str | None:
    """Parsed from the analog_sdk wheel filename in the url line —
    the same place brew scans the version from."""
    if not FORMULA.exists():
        return None
    match = re.search(r"analog_sdk-([0-9][^-]*)-py3-none-any\.whl", FORMULA.read_text())
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", help="analog-sdk version to pin")
    group.add_argument("--latest", action="store_true", help="pin PyPI's latest")
    group.add_argument(
        "--check",
        action="store_true",
        help="exit 1 (and print the lag) if the formula pins less than PyPI's latest",
    )
    args = parser.parse_args()

    if args.check:
        latest = pypi_latest_version(PACKAGE)
        pinned = formula_pinned_version()
        if pinned == latest:
            print(f"formula is current: {pinned}")
            return 0
        print(f"formula lags: pinned={pinned} latest={latest}")
        return 1

    version = pypi_latest_version(PACKAGE) if args.latest else args.version
    FORMULA.write_text(render(version))
    print(f"wrote {FORMULA} pinning {PACKAGE}=={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
