"""No network outside container loopback, no desktop or GPU, original fixtures only."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path

import collection_context
from collection_context.infrastructure.browser import BrowserSession

LOCAL_FLAGS = {
    "RUN_LOCAL_MEDIA_RUNTIME": "1",
    "RUN_LOCAL_HTTPS_SMOKE": "1",
    "RUN_LOCAL_BROWSER_LEASE": "1",
    "RUN_LOCAL_SEARCH_UI": "1",
}


def junit_summary(path: Path) -> dict:
    """Account for every reported case; platform skips never become passing proof."""
    cases = ET.parse(path).getroot().findall(".//testcase")
    if not cases:
        raise ValueError("JUnit contains no executed/collected cases")
    skipped = []
    failed = errors = 0
    for case in cases:
        failed += int(case.find("failure") is not None)
        errors += int(case.find("error") is not None)
        skip = case.find("skipped")
        if skip is not None:
            expected = (
                case.get("classname", "").endswith("test_context_system_native_roundtrip")
                and case.get("name") == "test_native_system_configuration_reopen_and_exact_cleanup"
            )
            skipped.append(
                {
                    "class": case.get("classname"),
                    "test": case.get("name"),
                    "reason": skip.get("message", "unspecified"),
                    "expected_on_linux": expected,
                    "scope": "macOS Keychain only; Linux system credential capability remains unverified"
                    if expected
                    else "Unexpected omission; acceptance is incomplete",
                }
            )
    return {
        "total": len(cases),
        "run": len(cases) - len(skipped),
        "passed": len(cases) - len(skipped) - failed - errors,
        "failed": failed,
        "errors": errors,
        "skipped": len(skipped),
        "skipped_cases": skipped,
        "unexpected_skips": sum(not row["expected_on_linux"] for row in skipped),
    }


def installed_browser() -> Path:
    """Ask the installed pinned SDK for its explicit path, never download a browser."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        path = Path(runtime.chromium.executable_path)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("Installed pinned Chromium is required; no download or skip fallback")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--expected-machine", choices=("aarch64", "x86_64"), required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != args.expected_machine:
        raise RuntimeError(
            "Run on the explicit Linux target architecture; host fixtures are not native proof"
        )
    if not args.output.is_absolute() or args.output.is_symlink():
        raise RuntimeError("Use an explicit ordinary empty output directory")
    if args.output.exists() and any(args.output.iterdir()):
        raise RuntimeError("Output is not empty; preserve previous evidence")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {
        "system": platform.platform(),
        "machine": platform.machine(),
        "expected_machine": args.expected_machine,
        "python": platform.python_version(),
        "uid": os.getuid(),
        "display": os.environ.get("DISPLAY"),
        "installed_module": collection_context.__file__,
        "dependencies": {
            name: version(name)
            for name in ("pytest", "mcp", "fastapi", "playwright", "rapidocr", "onnxruntime")
        },
        "steps": [],
        "private_data_read": False,
        "real_platform_requests": 0,
        "real_cloud_requests": 0,
        "limitations": [
            "Original synthetic fixtures only",
            "No real Douyin authentication/private source validation",
            "No cloud extraction quality or external AI client proof",
        ],
    }
    if os.getuid() == 0 or os.environ.get("DISPLAY"):
        raise RuntimeError("Acceptance requires non-root and no DISPLAY")
    for name in ("ffmpeg", "ffprobe", "openssl"):
        if shutil.which(name) is None:
            raise RuntimeError("Explicit installed media/TLS tools are required; no skip fallback")
    browser_binary = installed_browser()
    report["browser_binary"] = str(browser_binary)
    report["local_native_flags"] = LOCAL_FLAGS
    report["linux_system_credentials_verified"] = False
    root = Path(__file__).resolve().parents[2]
    font = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
    commands = [
        (
            "unit",
            [
                sys.executable,
                "-m",
                "pytest",
                str(root / "tests"),
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(args.output / "pytest-tmp"),
                "--junitxml",
                str(args.output / "pytest-junit.xml"),
            ],
            600,
            {
                "PYTHONPATH": os.pathsep.join((str(root), str(root / "src"))),
                "COLLECTION_CONTEXT_TEST_BROWSER_BINARY": str(browser_binary),
                "RUN_LOCAL_SYSTEM_KEYCHAIN": "0",
                **LOCAL_FLAGS,
            },
        ),
        (
            "query",
            [sys.executable, str(root / "tools/query_smoke.py"), "--output", str(args.output / "query")],
            40,
            {},
        ),
        (
            "writer",
            [sys.executable, str(root / "tools/writer_smoke.py"), "--output", str(args.output / "writer")],
            40,
            {},
        ),
        (
            "mcp",
            [sys.executable, str(root / "tools/mcp_smoke.py"), "--output", str(args.output / "mcp")],
            40,
            {},
        ),
        (
            "web",
            [sys.executable, str(root / "tools/web_smoke.py"), "--output", str(args.output / "web")],
            90,
            {},
        ),
        (
            "extraction",
            [
                sys.executable,
                str(root / "tools/extraction_smoke.py"),
                "--output",
                str(args.output / "extraction"),
                "--font",
                font,
                "--require-installed",
            ],
            180,
            {},
        ),
        (
            "media",
            [
                sys.executable,
                str(root / "tools/media_smoke.py"),
                "--output",
                str(args.output / "media"),
                "--font",
                font,
                "--models",
                str(args.models),
                "--require-installed",
            ],
            300,
            {},
        ),
    ]
    report_path = args.output / "linux-report.json"
    started = time.monotonic()
    for name, command, timeout, overrides in commands:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.update(overrides)
        before = time.monotonic()
        try:
            process = subprocess.run(
                command,
                cwd="/tmp",
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            result = {
                "name": name,
                "exit_code": process.returncode,
                "elapsed_seconds": time.monotonic() - before,
            }
            (args.output / f"{name}.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            if name == "unit":
                try:
                    counts = junit_summary(args.output / "pytest-junit.xml")
                    result["pytest"] = counts
                    if (
                        counts["unexpected_skips"]
                        or counts["failed"]
                        or counts["errors"]
                        or not counts["run"]
                    ):
                        result["pytest_exit_code"] = process.returncode
                        result["exit_code"] = 1
                except (OSError, ET.ParseError, ValueError):
                    result["junit_report_invalid"] = True
                    result["exit_code"] = 1
        except subprocess.TimeoutExpired:
            result = {
                "name": name,
                "exit_code": None,
                "timeout": True,
                "elapsed_seconds": time.monotonic() - before,
            }
        report["steps"].append(result)
        report["result"] = "failed" if result["exit_code"] != 0 else "in_progress"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        if result["exit_code"] != 0:
            return 1
    # Own browser lifecycle, not imported cookies and not a real platform request.
    try:
        with BrowserSession(args.output / "own-browser", headless=True) as browser:
            page = browser.context.new_page()
            page.set_content("<main>原创无桌面浏览器样例</main>")
            assert page.locator("main").inner_text() == "原创无桌面浏览器样例"
            page.close()
    except Exception:
        report["result"] = "failed"
        report["owned_headless_browser"] = False
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    report["owned_headless_browser"] = True
    report["result"] = "passed"
    report["elapsed_seconds"] = time.monotonic() - started
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "result": "passed"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
