#!/usr/bin/env python3
"""
Build script for merging sing-box .srs rule-set files.

Downloads .srs sources listed in sources/geosite.yaml and sources/geoip.yaml,
decompiles them to JSON, merges rules with deduplication, compiles back to .srs,
and writes the results to dist/.

Usage:
    python scripts/build.py [--sing-box-path PATH] [--work-dir DIR]

Environment variables:
    SING_BOX_VERSION   Pin to a specific release tag (e.g. "1.11.0").
                       If unset, the latest stable release is fetched from GitHub.
    SING_BOX_PATH      Path to an existing sing-box binary (skips download).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT_DIR / "sources"
DIST_DIR = ROOT_DIR / "dist"
DOWNLOAD_TIMEOUT = 60  # seconds per file
MAX_RETRIES = 3
MAX_WORKERS = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("build")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Source:
    name: str
    url: str
    category: str  # "geosite" or "geoip"


@dataclass
class SourceResult:
    source: Source
    success: bool
    rules: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


@dataclass
class BuildReport:
    timestamp: str
    sing_box_version: str
    categories: dict[str, CategoryReport] = field(default_factory=dict)


@dataclass
class CategoryReport:
    included_sources: list[str] = field(default_factory=list)
    failed_sources: list[dict[str, str]] = field(default_factory=list)
    rule_count: int = 0


# ---------------------------------------------------------------------------
# sing-box installation
# ---------------------------------------------------------------------------

def detect_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()

    os_name = {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(system)
    if os_name is None:
        raise RuntimeError(f"Unsupported OS: {system}")

    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    arch = arch_map.get(machine)
    if arch is None:
        raise RuntimeError(f"Unsupported architecture: {machine}")

    return os_name, arch


def fetch_latest_sing_box_version() -> str:
    url = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
    req = Request(url, headers={"Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    tag = data["tag_name"]
    return tag.lstrip("v")


def download_sing_box(version: str, work_dir: Path) -> Path:
    os_name, arch = detect_platform()
    ext = "zip" if os_name == "windows" else "tar.gz"
    archive_name = f"sing-box-{version}-{os_name}-{arch}"
    url = (
        f"https://github.com/SagerNet/sing-box/releases/download/"
        f"v{version}/{archive_name}.{ext}"
    )
    checksum_url = (
        f"https://github.com/SagerNet/sing-box/releases/download/"
        f"v{version}/sing-box-{version}-{os_name}-{arch}.{ext}.sha256sum"
    )

    archive_path = work_dir / f"{archive_name}.{ext}"
    log.info("Downloading sing-box %s for %s/%s ...", version, os_name, arch)
    _download(url, archive_path)

    try:
        log.info("Verifying SHA-256 checksum ...")
        _download(checksum_url, work_dir / "sha256sum.txt")
        expected = (work_dir / "sha256sum.txt").read_text().split()[0].strip()
        actual = _sha256(archive_path)
        if actual != expected:
            raise RuntimeError(
                f"Checksum mismatch for sing-box archive: expected {expected}, got {actual}"
            )
        log.info("Checksum OK.")
    except (HTTPError, URLError) as exc:
        log.warning("Could not fetch checksum file (%s), skipping verification.", exc)

    log.info("Extracting ...")
    extract_dir = work_dir / "sing-box-extract"
    extract_dir.mkdir(exist_ok=True)
    if ext == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(extract_dir)

    binary_name = "sing-box.exe" if os_name == "windows" else "sing-box"
    for p in extract_dir.rglob(binary_name):
        p.chmod(0o755)
        return p

    raise RuntimeError("sing-box binary not found in archive")


def get_sing_box(work_dir: Path) -> Path:
    env_path = os.environ.get("SING_BOX_PATH")
    if env_path:
        p = Path(env_path)
        if not p.exists():
            raise FileNotFoundError(f"SING_BOX_PATH={env_path} does not exist")
        return p

    which = shutil.which("sing-box")
    if which:
        return Path(which)

    version = os.environ.get("SING_BOX_VERSION") or fetch_latest_sing_box_version()
    log.info("Resolved sing-box version: %s", version)
    return download_sing_box(version, work_dir)


def sing_box_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "version"],
        capture_output=True, text=True, check=True,
    )
    first_line = result.stdout.strip().splitlines()[0]
    parts = first_line.split()
    for part in parts:
        if part[0].isdigit():
            return part
    return first_line


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "customsrs-builder/1.0"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        dest.write_bytes(resp.read())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_srs(source: Source, dest_dir: Path) -> Path:
    dest = dest_dir / f"{source.category}_{source.name}.srs"
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _download(source.url, dest)
            return dest
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            last_exc = exc
            log.warning(
                "  [%s/%s] %s attempt %d/%d failed: %s",
                source.category, source.name, source.url, attempt, MAX_RETRIES, exc,
            )
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {last_exc}")


# ---------------------------------------------------------------------------
# Decompile / compile / merge
# ---------------------------------------------------------------------------

def decompile_srs(binary: Path, srs_path: Path, json_path: Path) -> None:
    subprocess.run(
        [str(binary), "rule-set", "decompile", str(srs_path), "-o", str(json_path)],
        capture_output=True, text=True, check=True,
    )


def compile_srs(binary: Path, json_path: Path, srs_path: Path) -> None:
    subprocess.run(
        [str(binary), "rule-set", "compile", str(json_path), "-o", str(srs_path)],
        capture_output=True, text=True, check=True,
    )


def rule_key(rule: dict[str, Any]) -> str:
    return json.dumps(rule, sort_keys=True, ensure_ascii=False)


def merge_rules(all_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for rule in all_rules:
        key = rule_key(rule)
        if key not in seen:
            seen.add(key)
            merged.append(rule)
    return merged


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------

def load_sources(category: str) -> list[Source]:
    path = SOURCES_DIR / f"{category}.yaml"
    if not path.exists():
        log.warning("Source file %s not found, skipping category.", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    entries = data.get("sources", [])
    return [Source(name=e["name"], url=e["url"], category=category) for e in entries]


# ---------------------------------------------------------------------------
# Per-source processing
# ---------------------------------------------------------------------------

def process_source(source: Source, binary: Path, work_dir: Path) -> SourceResult:
    try:
        log.info("[%s/%s] Downloading ...", source.category, source.name)
        srs_path = download_srs(source, work_dir)

        json_path = work_dir / f"{source.category}_{source.name}.json"
        log.info("[%s/%s] Decompiling ...", source.category, source.name)
        decompile_srs(binary, srs_path, json_path)

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        rules = data.get("rules", [])
        log.info("[%s/%s] OK — %d rules", source.category, source.name, len(rules))
        return SourceResult(source=source, success=True, rules=rules)

    except Exception as exc:
        log.error("[%s/%s] FAILED: %s", source.category, source.name, exc)
        return SourceResult(source=source, success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Category build
# ---------------------------------------------------------------------------

def build_category(
    category: str,
    sources: list[Source],
    binary: Path,
    work_dir: Path,
) -> CategoryReport:
    report = CategoryReport()

    results: list[SourceResult] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_source, src, binary, work_dir): src
            for src in sources
        }
        for future in as_completed(futures):
            results.append(future.result())

    all_rules: list[dict[str, Any]] = []
    for r in results:
        if r.success:
            report.included_sources.append(r.source.name)
            all_rules.extend(r.rules)
        else:
            report.failed_sources.append({"name": r.source.name, "error": r.error})

    merged = merge_rules(all_rules)
    report.rule_count = len(merged)
    log.info(
        "[%s] Merged: %d rules from %d sources (%d failed)",
        category, len(merged), len(report.included_sources), len(report.failed_sources),
    )

    if not merged:
        log.warning("[%s] No rules collected — skipping output.", category)
        return report

    merged_json = {"version": 3, "rules": merged}
    json_out = work_dir / f"{category}-merged.json"
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(merged_json, f, ensure_ascii=False)

    srs_out = DIST_DIR / f"{category}-merged.srs"
    log.info("[%s] Compiling → %s", category, srs_out)
    compile_srs(binary, json_out, srs_out)

    return report


# ---------------------------------------------------------------------------
# Manifest & README generation
# ---------------------------------------------------------------------------

def write_manifest(report: BuildReport) -> None:
    obj = {
        "timestamp": report.timestamp,
        "sing_box_version": report.sing_box_version,
        "categories": {},
    }
    for cat, cr in report.categories.items():
        obj["categories"][cat] = {
            "included_sources": sorted(cr.included_sources),
            "failed_sources": cr.failed_sources,
            "rule_count": cr.rule_count,
        }
    path = DIST_DIR / "manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    log.info("Manifest written to %s", path)


def write_dist_readme(report: BuildReport) -> None:
    lines = [
        "# Build Report",
        "",
        f"**Built at:** {report.timestamp}  ",
        f"**sing-box version:** {report.sing_box_version}",
        "",
    ]
    for cat, cr in sorted(report.categories.items()):
        lines.append(f"## {cat}")
        lines.append("")
        lines.append(f"**Rules in merged file:** {cr.rule_count}")
        lines.append("")
        if cr.included_sources:
            lines.append("**Included sources:**")
            for name in sorted(cr.included_sources):
                lines.append(f"- {name}")
            lines.append("")
        if cr.failed_sources:
            lines.append("**⚠️ Failed sources:**")
            for entry in cr.failed_sources:
                lines.append(f"- **{entry['name']}**: {entry['error']}")
            lines.append("")

    path = DIST_DIR / "README.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("dist/README.md written.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build merged sing-box rule-sets")
    parser.add_argument(
        "--sing-box-path", type=str, default=None,
        help="Path to sing-box binary (overrides auto-download)",
    )
    parser.add_argument(
        "--work-dir", type=str, default=None,
        help="Temporary working directory (default: auto-created in system temp)",
    )
    args = parser.parse_args()

    if args.sing_box_path:
        os.environ["SING_BOX_PATH"] = args.sing_box_path

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="srs-build-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    log.info("Work directory: %s", work_dir)

    try:
        binary = get_sing_box(work_dir)
        log.info("Using sing-box at: %s", binary)
        sb_version = sing_box_version(binary)
        log.info("sing-box version: %s", sb_version)

        report = BuildReport(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sing_box_version=sb_version,
        )

        has_failures = False
        for category in ("geosite", "geoip"):
            sources = load_sources(category)
            if not sources:
                log.info("[%s] No sources configured, skipping.", category)
                continue
            cat_report = build_category(category, sources, binary, work_dir)
            report.categories[category] = cat_report
            if cat_report.failed_sources:
                has_failures = True

        write_manifest(report)
        write_dist_readme(report)

        if has_failures:
            log.warning("Build completed WITH WARNINGS — some sources failed.")
            return 2  # partial success
        log.info("Build completed successfully.")
        return 0

    finally:
        if not args.work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
