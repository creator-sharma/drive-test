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
        - smartctl (smartmontools) if installed (and USB bridge exposes SMART)

It only writes inside a test folder and cleans up by default (use --keep to retain).
"""

import os
import sys
import time
import argparse
import hashlib
import shutil
import random
import statistics
import subprocess
from typing import Optional, Dict, Tuple

__version__ = "1.2.0"


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


def powershell_disk_health(drive_letter: str) -> Optional[int]:
    """Print PowerShell health and return the PhysicalDrive number if found."""
    dl = drive_letter.upper().rstrip("\\/").replace(":", "")
    if not dl or len(dl) != 1 or not dl.isalpha():
        print("[PS   ] Skipping PowerShell disk mapping (invalid drive letter).")
        return None

    # Map drive letter -> Disk Number
    ps_map = (
        f"$p = Get-Partition -DriveLetter {dl} -ErrorAction SilentlyContinue; "
        f"if ($p) {{ ($p | Get-Disk | Select-Object -First 1 Number).Number }} "
    )
    num = _run_powershell(ps_map)
    disk_number = None
    if num and num.strip().isdigit():
        disk_number = int(num.strip())

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
        else:
            print(
                "[PS   ] Reliability counters not available (non-admin or bridge doesn’t expose)."
            )
    else:
        print("[PS   ] Could not map drive letter to a Disk Number.")

    return disk_number


def smartctl_summary(drive_letter: str, disk_number_hint: Optional[int]) -> None:
    """Try smartctl. Prefers PhysicalDriveN if we know N."""
    candidates = []
    if isinstance(disk_number_hint, int):
        candidates.append(rf"\\.\PhysicalDrive{disk_number_hint}")
    drv = drive_letter.upper().rstrip("\\/")
    candidates.append(drv if drv.endswith(":") else (drv + ":"))

    for target in candidates:
        try:
            out = subprocess.check_output(
                ["smartctl", "-a", target], text=True, stderr=subprocess.STDOUT
            )
            print(f"[SMART] smartctl on {target} (key lines):")
            key_lines = []
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
            if key_lines:
                print("\n".join(key_lines[:60]))
            else:
                print("(smartctl ran; no standard attributes recognized in output)")
            return
        except FileNotFoundError:
            print(
                "[SMART] smartctl not found. Install smartmontools to view detailed SMART."
            )
            return
        except subprocess.CalledProcessError as e:
            msg = e.output.strip()
            print(f"[SMART] smartctl error on {target}: {msg[:300]}")
            continue


def write_test_file(
    path: str, size_gb: float = 2.0, chunk_mb: int = 64, pattern: str = "random"
) -> Tuple[str, float]:
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
            if pattern == "zeros":
                buf = bytes(n)
            else:
                buf = os.urandom(n)
            f.write(buf)
            h.update(buf)
            wrote += n
        f.flush()
        os.fsync(f.fileno())
    dt = time.perf_counter() - t0
    w_speed = wrote / dt / (1024**2)
    print(f"[WRITE] Wrote {human_bytes(wrote)} in {dt:.2f}s  →  {w_speed:.1f} MB/s")
    return h.hexdigest(), w_speed


def read_verify(path: str, chunk_mb: int = 64) -> Tuple[str, float]:
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
    return h.hexdigest(), r_speed


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


def main():
    ap = argparse.ArgumentParser(
        description="External HDD sanity + performance test (Windows)"
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
        "--file-name",
        default="testfile.bin",
        help="Name of test file (default: testfile.bin)",
    )
    args = ap.parse_args()

    print(f"=== External HDD Test v{__version__} ===")
    print(
        f"[CONF ] Target drive: {args.drive} | size: {args.size_gb} GB | chunk: {args.chunk_mb} MB | pattern: {args.pattern}"
    )

    root = check_drive_root(args.drive)
    test_dir = os.path.join(root, "HDD_Test")
    os.makedirs(test_dir, exist_ok=True)
    test_path = os.path.join(test_dir, args.file_name)

    basic_disk_info(root)

    # PowerShell health + get PhysicalDrive number (if possible)
    disk_number = powershell_disk_health(args.drive)

    # smartctl (if present)
    smartctl_summary(args.drive, disk_number)

    # Ensure enough free space for write step (unless verify-only)
    if not args.verify_only:
        total, used, free = shutil.disk_usage(root)
        need = int(args.size_gb * (1024**3)) + (128 * 1024**2)
        if free < need:
            raise SystemExit(
                f"Not enough free space on {root}. Need ~{human_bytes(need)}, have {human_bytes(free)}."
            )

    if args.verify_only:
        if not os.path.exists(test_path):
            raise SystemExit(
                f"[ERR  ] {test_path} not found. Run once without --verify-only (use --keep) to create it."
            )
        print("[MODE ] Verify-only: skipping write; hashing existing file.")
        exp_hash = None
    else:
        exp_hash, w_speed = write_test_file(
            test_path, args.size_gb, args.chunk_mb, args.pattern
        )

    got_hash, r_speed = read_verify(test_path, args.chunk_mb)

    if exp_hash is not None:
        print(f"[HASH ] Expected: {exp_hash}")
        print(f"[HASH ]   Actual: {got_hash}")
        if exp_hash == got_hash:
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

    # Heuristic hint if results look cached
    if exp_hash is not None and looks_cached(w_speed, r_speed, rr):
        print("\n[NOTE ] Your read/latency results look heavily OS-cached.")
        print("        For more realistic read numbers, try one of these:")
        print("          1) Re-run with a larger file (e.g., --size-gb 8 or 16), or")
        print(
            "          2) Run once with --keep, unplug/replug the drive, then run with --verify-only, or"
        )
        print("          3) Reboot and run with --verify-only on the existing file.")
    elif exp_hash is None and rr and rr.get("avg_ms", 0) < 0.15:
        print(
            "\n[NOTE ] Extremely low random latencies suggest results may still be cached by Windows."
        )

    if not args.keep and not args.verify_only:
        try:
            os.remove(test_path)
            if not os.listdir(test_dir):
                os.rmdir(test_dir)
        except Exception:
            pass

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
        " - Typical USB 3.x external HDD seq speeds: ~100–200 MB/s. Much lower may indicate USB 2.0 or cabling issues."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ABORT] Interrupted by user.")
