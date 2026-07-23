"""Ensure a local sing-box binary matching do_egress.singbox_version.

Downloads from GitHub releases into output/do_egress/bin/ when missing or
version-mismatched. Register code only needs the resolved exe path.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from grok_register.do_egress.settings import DoEgressSettings
from grok_register.paths import PROJECT_ROOT

LogFn = Callable[[str], None]

_GITHUB = "https://github.com/SagerNet/sing-box/releases/download"


def _log(msg: str, log: LogFn | None) -> None:
    if log:
        log(msg)
    else:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _platform_asset(version: str) -> tuple[str, str]:
    """Return (asset_filename, member_name_hint) for current OS/arch."""
    ver = version.lstrip("v")
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    else:
        raise RuntimeError(f"unsupported machine arch for sing-box: {machine}")

    if system == "windows":
        # e.g. sing-box-1.11.15-windows-amd64.zip
        name = f"sing-box-{ver}-windows-{arch}.zip"
        return name, "sing-box.exe"
    if system == "darwin":
        name = f"sing-box-{ver}-darwin-{arch}.tar.gz"
        return name, "sing-box"
    if system == "linux":
        name = f"sing-box-{ver}-linux-{arch}.tar.gz"
        return name, "sing-box"
    raise RuntimeError(f"unsupported OS for sing-box: {system}")


def managed_bin_dir(settings: DoEgressSettings) -> Path:
    p = Path(settings.state_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p / "bin"


def managed_exe_path(settings: DoEgressSettings) -> Path:
    name = "sing-box.exe" if platform.system().lower() == "windows" else "sing-box"
    return managed_bin_dir(settings) / name


def managed_version_stamp(settings: DoEgressSettings) -> Path:
    return managed_bin_dir(settings) / "VERSION"


def _download(url: str, dest: Path, log: LogFn | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    _log(f"[egress] downloading {url}", log)
    req = Request(url, headers={"User-Agent": "grok-register-do-egress"})
    with urlopen(req, timeout=180) as resp, open(tmp, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    tmp.replace(dest)
    _log(f"[egress] downloaded {dest.name} ({total} bytes)", log)


def _extract_binary(archive: Path, want_name: str, out_exe: Path, log: LogFn | None) -> None:
    out_exe.parent.mkdir(parents=True, exist_ok=True)
    found: Path | None = None
    with tempfile.TemporaryDirectory(prefix="singbox-") as td:
        td_path = Path(td)
        if archive.suffix == ".zip" or archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive, "r") as zf:
                zf.extractall(td_path)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(td_path)

        candidates = list(td_path.rglob(want_name))
        if not candidates:
            # fallback: any sing-box / sing-box.exe
            candidates = list(td_path.rglob("sing-box.exe")) + list(td_path.rglob("sing-box"))
        if not candidates:
            raise RuntimeError(f"sing-box binary not found inside {archive.name}")
        found = candidates[0]
        if out_exe.exists():
            try:
                out_exe.unlink()
            except Exception:
                pass
        shutil.copy2(found, out_exe)

    # executable bit on unix
    if platform.system().lower() != "windows":
        mode = out_exe.stat().st_mode
        out_exe.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _log(f"[egress] installed {out_exe}", log)


def _probe_version(exe: Path) -> str:
    try:
        r = subprocess.run(
            [str(exe), "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        text = (r.stdout or "") + "\n" + (r.stderr or "")
        # typical: sing-box version 1.11.15
        for line in text.splitlines():
            line = line.strip()
            if "version" in line.lower():
                parts = line.replace("\t", " ").split()
                for i, p in enumerate(parts):
                    if p.lower() == "version" and i + 1 < len(parts):
                        return parts[i + 1].lstrip("v")
                # last token often is version
                if parts:
                    return parts[-1].lstrip("v")
        return ""
    except Exception:
        return ""


def _is_usable_exe(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return os.access(path, os.X_OK) or platform.system().lower() == "windows"
    except Exception:
        return path.is_file()


def resolve_singbox_exe(
    settings: DoEgressSettings,
    *,
    log: LogFn | None = None,
    force_download: bool = False,
) -> str:
    """Return path to sing-box for the configured version (download if needed).

    Priority:
      1. Managed bin under state_dir/bin (auto-download for singbox_version)
      2. Explicit absolute/relative path in settings.singbox_exe if it exists
      3. PATH lookup for 'sing-box' / 'sing-box.exe'
    Always prefers managed binary matching singbox_version when auto_download applies.
    """
    ver = (settings.singbox_version or "1.11.15").lstrip("v")
    managed = managed_exe_path(settings)
    stamp = managed_version_stamp(settings)

    need = force_download
    if not managed.is_file():
        need = True
    elif stamp.is_file():
        if stamp.read_text(encoding="utf-8").strip().lstrip("v") != ver:
            need = True
    else:
        probed = _probe_version(managed)
        if probed and probed != ver:
            need = True
        elif not probed:
            need = True

    if need:
        asset, member = _platform_asset(ver)
        url = f"{_GITHUB}/v{ver}/{asset}"
        archive_path = managed_bin_dir(settings) / asset
        try:
            _download(url, archive_path, log)
            _extract_binary(archive_path, member, managed, log)
            stamp.write_text(ver + "\n", encoding="utf-8")
            try:
                archive_path.unlink()
            except Exception:
                pass
        except Exception as exc:
            # If managed download fails but user path works, fall through
            _log(f"[egress] sing-box download failed: {exc}", log)
            if not managed.is_file():
                raise RuntimeError(
                    f"failed to download sing-box v{ver}: {exc}"
                ) from exc

    if managed.is_file():
        got = _probe_version(managed)
        _log(f"[egress] sing-box ready: {managed} ({got or ver})", log)
        return str(managed.resolve())

    # Fallback: configured path
    configured = (settings.singbox_exe or "").strip()
    if configured and configured not in ("sing-box", "sing-box.exe"):
        p = Path(configured)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if _is_usable_exe(p):
            return str(p.resolve())

    which = shutil.which("sing-box") or shutil.which("sing-box.exe")
    if which:
        return which

    raise RuntimeError(
        f"sing-box v{ver} not available; managed path missing: {managed}"
    )


def ensure_singbox_for_settings(
    settings: DoEgressSettings,
    *,
    log: LogFn | None = None,
) -> DoEgressSettings:
    """Mutate settings.singbox_exe to a concrete downloaded/resolved path."""
    path = resolve_singbox_exe(settings, log=log)
    settings.singbox_exe = path
    return settings
