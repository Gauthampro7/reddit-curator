"""Multi-browser CDP launcher for Fedora/Linux (native RPM binaries and Flatpaks)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

import requests

_NATIVE_BROWSERS: tuple[tuple[str, str], ...] = (
    ("google-chrome", "Google Chrome"),
    ("google-chrome-stable", "Google Chrome Stable"),
    ("chromium", "Chromium"),
    ("chromium-browser", "Chromium Browser"),
    ("brave-browser", "Brave Browser"),
)

_FLATPAK_BROWSERS: tuple[tuple[str, str], ...] = (
    ("com.google.Chrome", "Google Chrome (Flatpak)"),
    ("com.brave.Browser", "Brave Browser (Flatpak)"),
    ("org.chromium.Chromium", "Chromium (Flatpak)"),
)


def _ensure_private_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved, 0o700)
    if resolved.parent.name == "reddit-curator":
        os.chmod(resolved.parent, 0o700)
    return resolved


def _probe_cdp_ready(cdp_http_url: str, timeout: float = 1.0) -> bool:
    try:
        resp = requests.get(f"{cdp_http_url}/json/version", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return isinstance(data, dict)
    except (requests.RequestException, ValueError):
        return False
    return False


class BrowserLauncher:
    """Detect and launch Chromium-based browsers with isolated loopback CDP."""

    @classmethod
    def detect_browsers(cls) -> list[dict[str, str]]:
        """Return installed native RPM and Flatpak Chromium-based browsers."""
        detected: list[dict[str, str]] = []

        for binary, label in _NATIVE_BROWSERS:
            exe_path = shutil.which(binary)
            if exe_path:
                detected.append(
                    {
                        "browser_id": binary,
                        "kind": "native",
                        "executable": exe_path,
                        "name": label,
                    }
                )

        flatpak_bin = shutil.which("flatpak")
        if flatpak_bin:
            for app_id, label in _FLATPAK_BROWSERS:
                try:
                    proc = subprocess.run(
                        [flatpak_bin, "info", app_id],
                        capture_output=True,
                        text=True,
                        timeout=5.0,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                if proc.returncode == 0:
                    detected.append(
                        {
                            "browser_id": app_id,
                            "kind": "flatpak",
                            "executable": flatpak_bin,
                            "name": label,
                        }
                    )

        return detected

    @classmethod
    def _select_browser(
        cls,
        browsers: list[dict[str, str]],
        preferred: str | None,
    ) -> dict[str, str]:
        if not browsers:
            raise FileNotFoundError(
                "No supported Chromium/Chrome/Brave browser found (checked RPM binaries and Flatpak)."
            )
        if not preferred:
            return browsers[0]

        target = preferred.strip().lower()
        for entry in browsers:
            if entry["browser_id"].lower() == target:
                return entry
        raise FileNotFoundError(
            f"Preferred browser {preferred!r} is not installed. Available: {[b['browser_id'] for b in browsers]}"
        )

    @classmethod
    def build_launch_command(
        cls,
        browser: dict[str, str],
        port: int,
        user_data_dir: Path,
        *,
        headless: bool = False,
        start_url: str = "https://www.reddit.com/",
    ) -> list[str]:
        """Construct the argv list for launching either a native binary or Flatpak browser."""
        flags: list[str] = [
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if os.environ.get("WAYLAND_DISPLAY"):
            flags.append("--ozone-platform-hint=auto")
        if headless:
            flags.append("--headless=new")
        if start_url:
            flags.append(start_url)

        if browser["kind"] == "flatpak":
            return [
                browser["executable"],
                "run",
                f"--filesystem={user_data_dir}:create",
                browser["browser_id"],
                *flags,
            ]
        return [browser["executable"], *flags]

    @classmethod
    def launch(
        cls,
        port: int = 9222,
        preferred: str | None = None,
        *,
        user_data_dir: Path | None = None,
        headless: bool = False,
        wait_timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Launch or reuse a CDP-enabled browser bound strictly to 127.0.0.1."""
        if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError(f"Invalid CDP port: {port!r}")

        raw_profile_dir = (
            Path(user_data_dir)
            if user_data_dir is not None
            else Path.home() / ".config" / "reddit-curator" / "chrome-profile"
        )
        resolved_dir = _ensure_private_dir(raw_profile_dir)
        cdp_http_url = f"http://127.0.0.1:{port}"

        if _probe_cdp_ready(cdp_http_url, timeout=1.0):
            return {
                "launched": False,
                "reused_existing": True,
                "browser_id": "existing",
                "command": [],
                "pid": None,
                "port": port,
                "cdp_http_url": cdp_http_url,
                "user_data_dir": str(resolved_dir),
            }

        browsers = cls.detect_browsers()
        selected = cls._select_browser(browsers, preferred)
        command = cls.build_launch_command(selected, port, resolved_dir, headless=headless)

        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        deadline = time.monotonic() + max(0.5, float(wait_timeout))
        while time.monotonic() < deadline:
            if _probe_cdp_ready(cdp_http_url, timeout=0.5):
                return {
                    "launched": True,
                    "reused_existing": False,
                    "browser_id": selected["browser_id"],
                    "command": command,
                    "pid": proc.pid,
                    "port": port,
                    "cdp_http_url": cdp_http_url,
                    "user_data_dir": str(resolved_dir),
                }
            exit_code = proc.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"Browser {selected['browser_id']} exited with code {exit_code} before CDP became ready."
                )
            time.sleep(0.25)

        raise TimeoutError(
            f"Browser {selected['browser_id']} did not expose CDP on {cdp_http_url} within {wait_timeout}s."
        )
