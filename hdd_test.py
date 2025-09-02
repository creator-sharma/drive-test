#!/usr/bin/env python3
r"""
External HDD sanity + performance test (Windows-friendly)

What it does:
  • Creates E:\HDD_Test\testfile.bin (or your chosen drive)
  • Sequential write + read with checksum verification (BLAKE2b)
  • Reports sequential MB/s (write & read)
  • Random 4KiB read probes (avg / p95 latency, rough throughput)
  • Best-effort disk health:
        - PowerShell: Get-Disk / Get-StorageReliabilityCounter (if available)
        - smartctl (smartmontools) if installed; auto-tries -d sat / -d scsi for USB enclosures
  • JSON report via --report-json <path> (JSON Lines)

Interactive prompts if you run with no arguments. Writes only inside HDD_Test and cleans up by default.
"""

import argparse
import hashlib
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

__version__ = "1.5.0"

# ---------------------------- helpers ----------------------------


def human_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB", "PB", "EB"]:
        if n < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} ZB"


def check_drive_root(drive: str) -> str:
    drive = drive.upper().rstrip("\\/")
    if len(drive) == 2 and drive[1] == ":":
        root = drive + "\\"
    else:
        raise SystemExit(f"Drive must look like E:  (got: {drive})")
    if not os.path.isdir(root):
        raise SystemExit(f"{root} is not a valid/existing drive.")
    return root


def basic_disk_info(root: str) -> None:
    total, used, free = shutil.disk_usage(root)
    print(
        f"[INFO ] {root}  Total: {human_bytes(total)}  Free: {human_bytes(free)}  Used: {human_bytes(used)}"
    )
    print(f"[INFO ] Python {sys.version.split()[0]} on nt (CPython)")
    # WMIC is deprecated but often present; ignore failures
    try:
        out = subprocess.check_output(
            ["wmic", "diskdrive", "get", "Model,Status,Size,InterfaceType"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        print("[INFO ] WMIC diskdrive snapshot (may include all disks):")
        print(out.strip())
    except Exception:
        pass


def _run_powershell(cmd: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return None


def _parse_ps_kv_block(block: str) -> Dict[str, Optional[int]]:
    """Parse 'Key : Value' lines; convert numeric strings to int, empty -> None."""
    result: Dict[str, Optional[int]] = {}
    if not block:
        return result
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val == "":
            result[key] = None
        else:
            try:
                result[key] = int(val)
            except ValueError:
                # try float -> int if looks like number, else keep as string
                try:
                    fv = float(val)
                    result[key] = int(fv)
                except Exception:
                    # keep raw strin
                    result[key] = val  # pyright: ignore[reportArgumentType]
    return result


def powershell_disk_health(
    drive_letter: str,
) -> Tuple[Optional[int], Dict[str, Optional[int]]]:
    """Print PowerShell health and return (PhysicalDrive number, reliability counters dict)."""
    dl = drive_letter.upper().rstrip("\\/").replace(":", "")
    if not dl or len(dl) != 1 or not dl.isalpha():
        print("[PS   ] Skipping PowerShell disk mapping (invalid drive letter).")
        return None, {}

    # Map drive letter -> Disk Number
    ps_map = (
        f"$p = Get-Partition -DriveLetter {dl} -ErrorAction SilentlyContinue; "
        f"if ($p) {{ ($p | Get-Disk | Select-Object -First 1 Number).Number }} "
    )
    num = _run_powershell(ps_map)
    disk_number = int(num.strip()) if (num and num.strip().isdigit()) else None

    # Show details
    ps_details = (
        f"$d = (Get-Partition -DriveLetter {dl} | Get-Disk | Select-Object -First 1); "
        f"if ($d) {{ "
        f"$d | Select-Object Number, FriendlyName, Model, BusType, HealthStatus, Size | Format-List | Out-String "
        f"}}"
    )
    details = _run_powershell(ps_details)
    if details:
        print("[PS   ] Get-Disk details:")
        print(details.strip())

    # Reliability counters (need admin on many systems; USB bridges may not expose)
    reliability: Dict[str, Optional[int]] = {}
    if disk_number is not None:
        ps_rel = (
            f"$d = Get-Disk -Number {disk_number} -ErrorAction SilentlyContinue; "
            f"if ($d) {{ "
            f"Get-StorageReliabilityCounter -Disk $d -ErrorAction SilentlyContinue | "
            f"Select-Object Temperature, TemperatureMax, ReadErrorsTotal, WriteErrorsTotal, "
            f"Wear, StartStopCount, LoadUnloadCycleCount | Format-List | Out-String "
            f"}}"
        )
        rel = _run_powershell(ps_rel)
        if rel and rel.strip():
            print("[PS   ] Storage reliability counters:")
            print(rel.strip())
            reliability = _parse_ps_kv_block(rel)
        else:
            print(
                "[PS   ] Reliability counters not available (non-admin or bridge doesn’t expose)."
            )
    else:
        print("[PS   ] Could not map drive letter to a Disk Number.")

    return disk_number, reliability


def resolve_report_path(user_value: Optional[str], drive_letter: str) -> Optional[str]:
    if not user_value:
        return None

    v = user_value.strip().strip('"').strip("'")
    if not v:
        return None

    # Default base: script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    drv = drive_letter.upper().rstrip(":\\/") or "X"
    default_name = f"hdd_runs_{drv}.jsonl"

    # Handle "y"/"yes" as "use default file in script dir"
    if v.lower() in ("y", "yes", "true", "1"):
        return os.path.join(script_dir, default_name)

    # Expand env/user vars
    path = os.path.expandvars(os.path.expanduser(v))

    # If explicit directory (exists or trailing slash), drop file into it
    if path.endswith(("\\", "/")) or os.path.isdir(path):
        return os.path.join(path, default_name)

    # If has a file extension and it's .json/.jsonl -> keep as file
    root, ext = os.path.splitext(path)
    if ext.lower() in (".json", ".jsonl"):
        return path

    # Otherwise treat it as a directory base and put default file in it
    return os.path.join(path, default_name)


# ---------- smartctl helpers (auto-discovery and USB device type retries) ----------


def find_smartctl(user_path: Optional[str] = None) -> Optional[str]:
    # 1) explicit path
    if user_path and os.path.exists(user_path):
        return user_path
    # 2) PATH
    try:
        import shutil as _shutil

        p = _shutil.which("smartctl")
        if p:
            return p
    except Exception:
        pass
    # 3) common install locations
    candidates = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.append(os.path.join(pf, "smartmontools", "bin", "smartctl.exe"))
    candidates.append(os.path.join(pfx86, "smartmontools", "bin", "smartctl.exe"))
    candidates.append(r"C:\ProgramData\chocolatey\bin\smartctl.exe")
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


_SMART_OVERALL_PAT = re.compile(
    r"SMART (?:overall-health|Health Status).*?:\s*([A-Z]+)", re.IGNORECASE
)
_SMART_ATTR_PAT = re.compile(r"^\s*(\d+)\s+([A-Za-z0-9_\-]+)\s+.*?(-?\d+)\s*(?:\(|$)")


def smartctl_summary(
    drive_letter: str,
    disk_number_hint: Optional[int],
    smartctl_path: Optional[str] = None,
) -> Dict[str, object]:
    """Print smartctl summary and return a parsed dict of key info."""
    sc = find_smartctl(smartctl_path)
    if not sc:
        print(
            "[SMART] smartctl not found. Install smartmontools or add it to PATH, or pass --smartctl <path>."
        )
        return {}

    # Prefer PhysicalDriveN, then drive letter
    targets = []
    if isinstance(disk_number_hint, int):
        targets.append(rf"\\.\PhysicalDrive{disk_number_hint}")
    drv = drive_letter.upper().rstrip("\\/")
    targets.append(drv if drv.endswith(":") else (drv + ":"))

    def _run(args):
        return subprocess.check_output([sc] + args, text=True, stderr=subprocess.STDOUT)

    parsed: Dict[str, object] = {}
    for target in targets:
        for extra in ([], ["-d", "sat"], ["-d", "scsi"]):
            try:
                args = ["-a", target] + extra
                out = _run(args)
                trail = f" {' '.join(extra)}" if extra else ""
                print(f"[SMART] smartctl on {target}{trail} (key lines):")
                key_lines = []
                overall = None
                attrs: Dict[str, Dict[str, int]] = {}
                temp_c: Optional[int] = None
                for line in out.splitlines():
                    lat = line.strip()
                    if any(
                        k in lat
                        for k in [
                            "SMART overall-health",
                            "SMART Health Status",
                            "Reallocated_Sector_Ct",
                            "Reallocated Sector Count",
                            "Current_Pending_Sector",
                            "Current Pending Sector",
                            "Offline_Uncorrectable",
                            "UDMA_CRC_Error_Count",
                            "Reported_Uncorrect",
                            "Temperature_Celsius",
                            "Temperature",
                        ]
                    ):
                        key_lines.append(lat)
                    # parse overall
                    mo = _SMART_OVERALL_PAT.search(lat)
                    if mo:
                        overall = mo.group(1).upper()
                    # parse attribute rows
                    ma = _SMART_ATTR_PAT.match(lat)
                    if ma:
                        aid, name, raw = ma.groups()
                        try:
                            rawv = int(raw)
                        except Exception:
                            continue
                        attrs[aid] = {"name": name, "raw": rawv}  # pyright: ignore[reportArgumentType]
                        if name.lower().startswith("temperature"):
                            temp_c = rawv
                print(
                    "\n".join(key_lines[:60])
                    if key_lines
                    else "(Ran; no standard attributes detected)"
                )
                if overall:
                    parsed["overall"] = overall
                if temp_c is not None:
                    parsed["temperature_celsius"] = temp_c
                if attrs:
                    parsed["attributes"] = attrs
                return parsed
            except subprocess.CalledProcessError:
                continue
            except Exception:
                continue
    print(
        "[SMART] Could not read SMART via common methods (USB bridge may hide it). Try running as admin or a different enclosure."
    )
    return parsed


# -------------------------- I/O workers --------------------------


def write_test_file(
    path: str, size_gb: float = 2.0, chunk_mb: int = 64, pattern: str = "random"
) -> Tuple[str, float, float, int]:
    total_bytes = int(size_gb * (1024**3))
    chunk_size = int(chunk_mb * (1024**2))
    chunk_size = max(chunk_size, 1 * 1024 * 1024)
    print(
        f"[WRITE] Creating {human_bytes(total_bytes)} at {path} (chunk {human_bytes(chunk_size)}, pattern={pattern})"
    )

    h = hashlib.blake2b(digest_size=32)
    wrote = 0
    t0 = time.perf_counter()
    with open(path, "wb", buffering=0) as f:
        while wrote < total_bytes:
            n = min(chunk_size, total_bytes - wrote)
            buf = bytes(n) if pattern == "zeros" else os.urandom(n)
            f.write(buf)
            h.update(buf)
            wrote += n
        f.flush()
        os.fsync(f.fileno())
    dt = time.perf_counter() - t0
    w_speed = wrote / dt / (1024**2)
    print(f"[WRITE] Wrote {human_bytes(wrote)} in {dt:.2f}s  →  {w_speed:.1f} MB/s")
    return h.hexdigest(), w_speed, dt, wrote


def read_verify(path: str, chunk_mb: int = 64) -> Tuple[str, float, float, int]:
    size = os.path.getsize(path)
    h = hashlib.blake2b(digest_size=32)
    chunk_size = int(chunk_mb * (1024**2))
    chunk_size = max(chunk_size, 1 * 1024 * 1024)
    print(f"[READ ] Reading & hashing {human_bytes(size)} from {path}")

    read = 0
    t0 = time.perf_counter()
    with open(path, "rb", buffering=0) as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
            read += len(b)
    dt = time.perf_counter() - t0
    r_speed = read / dt / (1024**2)
    print(f"[READ ] Read {human_bytes(read)} in {dt:.2f}s  →  {r_speed:.1f} MB/s")
    return h.hexdigest(), r_speed, dt, read


def random_reads(
    path: str, samples: int = 400, block_size: int = 4096
) -> Optional[Dict[str, float]]:
    size = os.path.getsize(path)
    if size < block_size:
        return None
    offsets = [random.randrange(0, size - block_size) for _ in range(samples)]
    latencies = []
    tput_bytes = 0
    with open(path, "rb", buffering=0) as f:
        for off in offsets:
            t0 = time.perf_counter()
            f.seek(off)
            b = f.read(block_size)
            dt = time.perf_counter() - t0
            if not b or len(b) < block_size:
                continue
            latencies.append(dt)
            tput_bytes += len(b)
    if not latencies:
        return None
    avg_ms = statistics.fmean(latencies) * 1000
    p95_ms = (
        (statistics.quantiles(latencies, n=20)[18] * 1000)
        if len(latencies) >= 20
        else (max(latencies) * 1000)
    )
    tput_mb_s = tput_bytes / sum(latencies) / (1024**2)
    return {
        "avg_ms": avg_ms,
        "p95_ms": p95_ms,
        "throughput_mb_s": tput_mb_s,
        "samples": len(latencies),
    }


def looks_cached(
    write_mb_s: float, read_mb_s: float, rr: Optional[Dict[str, float]]
) -> bool:
    """Heuristic: very fast read + tiny random latency => likely OS cache."""
    if read_mb_s > max(250.0, write_mb_s * 2.5):
        if rr and rr.get("avg_ms", 9e9) < 0.15 and rr.get("throughput_mb_s", 0) > 150.0:
            return True
    return False


# ----------------------- interactive prompts -----------------------


def _prompt(text: str, default: Optional[str] = None) -> str:
    if default is None:
        return input(text).strip()
    else:
        s = input(f"{text} [{default}]: ").strip()
        return s if s else default


def _prompt_yes_no(text: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        s = input(f"{text} ({d}): ").strip().lower()
        if not s:
            return default
        if s in ("y", "yes"):
            return True
        if s in ("n", "no"):
            return False
        print("Please answer y or n.")


def interactive_config(defaults: argparse.Namespace) -> argparse.Namespace:
    print("\n--- Interactive Mode ---")
    # Drive
    while True:
        drive = _prompt("Drive letter (like E:)", defaults.drive)
        try:
            root = check_drive_root(drive)
            break
        except SystemExit as e:
            print(str(e))

    # Disk info for context
    total, used, free = shutil.disk_usage(root)
    free_gb = free / (1024**3)
    print(f"Detected {root} with ~{free_gb:.2f} GB free.")

    # Size GB (ensure it fits)
    while True:
        try:
            size_gb = float(_prompt("Test file size in GB", str(defaults.size_gb)))
        except ValueError:
            print("Enter a number (e.g., 2 or 4.5).")
            continue
        need = int(size_gb * (1024**3)) + (128 * 1024**2)
        if need > free:
            print(
                f"That's too large for the free space. Max suggested ≲ {max(0.0, free_gb - 0.2):.2f} GB."
            )
            continue
        break

    # Chunk MB
    while True:
        try:
            chunk_mb = int(_prompt("I/O chunk size in MB", str(defaults.chunk_mb)))
            if chunk_mb <= 0:
                raise ValueError
            break
        except ValueError:
            print("Enter a positive integer (e.g., 64).")

    # Pattern
    while True:
        pattern = _prompt(
            "Write pattern: 'random' or 'zeros'", defaults.pattern
        ).lower()
        if pattern in ("random", "zeros"):
            break
        print("Please type 'random' or 'zeros'.")

    # Random samples
    while True:
        try:
            random_samples = int(
                _prompt("Random 4KiB read samples", str(defaults.random_samples))
            )
            if random_samples <= 0:
                raise ValueError
            break
        except ValueError:
            print("Enter a positive integer (e.g., 400).")

    keep = _prompt_yes_no("Keep the test file after run?", defaults.keep)
    verify_only = _prompt_yes_no(
        "Verify-only (skip writing; only read/hash existing file)?",
        defaults.verify_only,
    )
    random_first = defaults.random_first
    if verify_only:
        random_first = _prompt_yes_no(
            "Run random 4KiB probe BEFORE sequential read (more realistic)?", True
        )
    file_name = _prompt("Test file name", defaults.file_name)
    smartctl_path = _prompt(
        "Path to smartctl.exe (Enter to auto-detect)", defaults.smartctl or ""
    )
    report_in = _prompt(
        "Save results to JSON (path or 'y' for default; Enter to skip)",
        getattr(defaults, "report_json", "") or "",
    )
    report_json = resolve_report_path(report_in, drive)

    ns = argparse.Namespace(
        drive=drive,
        size_gb=size_gb,
        chunk_mb=chunk_mb,
        pattern=pattern,
        random_samples=random_samples,
        keep=keep,
        verify_only=verify_only,
        random_first=random_first,
        file_name=file_name,
        smartctl=(smartctl_path if smartctl_path else None),
        report_json=(report_json if report_json else None),
    )
    return ns


# ------------------------------- main -------------------------------


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="External HDD sanity + performance test (Windows)", add_help=True
    )
    ap.add_argument("-d", "--drive", default="E:", help="Drive letter like E:")
    ap.add_argument(
        "--size-gb", type=float, default=2.0, help="Test file size in GB (default 2)"
    )
    ap.add_argument(
        "--chunk-mb", type=int, default=64, help="I/O chunk size in MB (default 64)"
    )
    ap.add_argument(
        "--pattern", choices=["random", "zeros"], default="random", help="Write pattern"
    )
    ap.add_argument(
        "--random-samples",
        type=int,
        default=400,
        help="Random 4KiB read samples (default 400)",
    )
    ap.add_argument(
        "--keep", action="store_true", help="Keep the test file (skip cleanup)"
    )
    ap.add_argument(
        "--verify-only",
        action="store_true",
        help="Do NOT write; only read & verify existing file if present",
    )
    ap.add_argument(
        "--random-first",
        action="store_true",
        help="(Verify-only) run random 4KiB probe BEFORE sequential read",
    )
    ap.add_argument(
        "--file-name",
        default="testfile.bin",
        help="Name of test file (default: testfile.bin)",
    )
    ap.add_argument("--smartctl", help="Path to smartctl.exe (optional)")
    ap.add_argument(
        "--report-json", help="Append one JSON object per run to this file (JSON Lines)"
    )
    ap.add_argument(
        "--no-interactive",
        action="store_true",
        help="Force non-interactive even if no args provided",
    )
    return ap


def _write_report_json(path: str, obj: Dict[str, object]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    print(f"[REPORT] Appended JSON to {path}")


def main():
    ap = build_argparser()

    # If no CLI args, drop into interactive prompts unless --no-interactive is given.
    if len(sys.argv) == 1:
        defaults = ap.parse_args([])  # get defaults
        defaults.random_first = True  # sensible default for verify-only
        args = interactive_config(defaults)
    else:
        args = ap.parse_args()
        if args.verify_only and "--random-first" not in sys.argv:
            args.random_first = True

        # Normalize --report-json even when passed via CLI
        if getattr(args, "report_json", None):
            args.report_json = resolve_report_path(args.report_json, args.drive)

    print(f"=== External HDD Test v{__version__} ===")
    print(
        f"[CONF ] Target drive: {args.drive} | size: {args.size_gb} GB | chunk: {args.chunk_mb} MB | pattern: {args.pattern}"
    )

    root = check_drive_root(args.drive)
    test_dir = os.path.join(root, "HDD_Test")
    os.makedirs(test_dir, exist_ok=True)
    test_path = os.path.join(test_dir, args.file_name)

    # capture disk usage snapshot for reporting
    du_total, du_used, du_free = shutil.disk_usage(root)
    basic_disk_info(root)

    # PowerShell health + get PhysicalDrive number (if possible)
    disk_number, ps_rel = powershell_disk_health(args.drive)

    # smartctl (if present)
    smart_info = smartctl_summary(args.drive, disk_number, args.smartctl)

    report: Dict[str, object] = {
        "timestamp_local": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "script_version": __version__,
        "python_version": sys.version.split()[0],
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "drive": args.drive,
        "file": test_path,
        "mode": ("verify-only" if args.verify_only else "write-read"),
        "params": {
            "size_gb": args.size_gb,
            "chunk_mb": args.chunk_mb,
            "pattern": args.pattern,
            "random_samples": args.random_samples,
            "random_first": args.random_first if args.verify_only else False,
            "keep": bool(args.keep),
        },
        "disk_usage_start": {"total": du_total, "used": du_used, "free": du_free},
        "powershell": {"reliability": ps_rel},
        "smart": smart_info or {},
    }

    if args.verify_only:
        if not os.path.exists(test_path):
            raise SystemExit(
                f"[ERR  ] {test_path} not found. Run once without --verify-only (use --keep) to create it."
            )
        print("[MODE ] Verify-only: will hash existing file.")
        rr = None
        rr_phase = None
        if args.random_first:
            rr = random_reads(test_path, samples=args.random_samples, block_size=4096)
            rr_phase = "pre-read"
            if rr:
                print(
                    f"[RAND ] 4KiB reads (pre-read): avg {rr['avg_ms']:.2f} ms, p95 {rr['p95_ms']:.2f} ms, ~{rr['throughput_mb_s']:.2f} MB/s over {rr['samples']} samples"
                )
            else:
                print("[RAND ] Random-read probe skipped/insufficient data")

        got_hash, r_speed, r_dt, r_bytes = read_verify(test_path, args.chunk_mb)

        if rr is None:
            rr = random_reads(test_path, samples=args.random_samples, block_size=4096)
            rr_phase = "post-read"
            if rr:
                print(
                    f"[RAND ] 4KiB reads: avg {rr['avg_ms']:.2f} ms, p95 {rr['p95_ms']:.2f} ms, ~{rr['throughput_mb_s']:.2f} MB/s over {rr['samples']} samples"
                )
            else:
                print("[RAND ] Random-read probe skipped/insufficient data")

        if rr and rr.get("avg_ms", 0) < 0.15:
            print(
                "\n[NOTE ] Extremely low random latencies suggest results may be cached by Windows."
            )

        report.update(
            {
                "write": None,
                "read": {
                    "bytes": r_bytes,
                    "seconds": r_dt,
                    "mb_per_s": r_speed,
                    "hash": got_hash,
                },
                "integrity": {
                    "expected_hash": None,
                    "actual_hash": got_hash,
                    "ok": None,
                },
                "random_read": {"phase": rr_phase, **(rr or {})} if rr else None,
            }
        )

    else:
        exp_hash, w_speed, w_dt, w_bytes = write_test_file(
            test_path, args.size_gb, args.chunk_mb, args.pattern
        )
        got_hash, r_speed, r_dt, r_bytes = read_verify(test_path, args.chunk_mb)

        print(f"[HASH ] Expected: {exp_hash}")
        print(f"[HASH ]   Actual: {got_hash}")
        ok = exp_hash == got_hash
        if ok:
            print("[OK   ] Read-after-write checksum matches ✅  (data integrity good)")
        else:
            print(
                "[FAIL ] Checksum mismatch ❌  (STOP using this drive; data corruption detected)"
            )

        rr = random_reads(test_path, samples=args.random_samples, block_size=4096)
        if rr:
            print(
                f"[RAND ] 4KiB reads: avg {rr['avg_ms']:.2f} ms, p95 {rr['p95_ms']:.2f} ms, ~{rr['throughput_mb_s']:.2f} MB/s over {rr['samples']} samples"
            )
        else:
            print("[RAND ] Random-read probe skipped/insufficient data")

        if looks_cached(w_speed, r_speed, rr):
            print("\n[NOTE ] Your read/latency results look heavily OS-cached.")
            print("        For more realistic read numbers, try one of these:")
            print(
                "          1) Re-run with a larger file (e.g., --size-gb 8 or 16), or"
            )
            print(
                "          2) Run once with --keep, unplug/replug the drive, then run with --verify-only --random-first, or"
            )
            print(
                "          3) Reboot and run with --verify-only --random-first on the existing file."
            )

        report.update(
            {
                "write": {
                    "bytes": w_bytes,
                    "seconds": w_dt,
                    "mb_per_s": w_speed,
                    "hash": exp_hash,
                    "pattern": args.pattern,
                },
                "read": {
                    "bytes": r_bytes,
                    "seconds": r_dt,
                    "mb_per_s": r_speed,
                    "hash": got_hash,
                },
                "integrity": {
                    "expected_hash": exp_hash,
                    "actual_hash": got_hash,
                    "ok": ok,
                },
                "random_read": {"phase": "post-read", **(rr or {})} if rr else None,
            }
        )

    if not args.keep and not args.verify_only:
        try:
            os.remove(test_path)
            if not os.listdir(test_dir):
                os.rmdir(test_dir)
        except Exception:
            pass

    # Write JSON report if requested
    if args.report_json:
        _write_report_json(args.report_json, report)

    print("\nTips:")
    print(
        " - Write speed is usually realistic; read speed can look high if data is cached by the OS."
    )
    print(
        " - For a quick surface/filesystem check, run:  chkdsk /scan E:   (replace E: if needed)."
    )
    print(
        " - SMART IDs to watch if smartctl works: 5, 187, 197, 198 should be 0; 199>0 often means bad USB cable/port."
    )
    print(
        " - Typical USB 3.x external HDD seq speeds: ~100-200 MB/s. Much lower may indicate USB 2.0 or cabling issues."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ABORT] Interrupted by user.")
