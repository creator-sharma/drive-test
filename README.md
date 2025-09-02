# HDD Test (Windows) — README

A small Windows-friendly Python script to sanity-check and benchmark a new external HDD.
It writes a large test file on your target drive (default `E:`), verifies integrity with a checksum, runs simple performance probes, and (best-effort) shows health info via PowerShell and SMART.

> Script path (your setup):
> `C:\Dev\Project_25\_scripts\drive-test\hdd_test.py`

---

## What it checks

- **Data integrity**: read-after-write checksum (BLAKE2b) must match.
- **Sequential performance**: write/read throughput (MB/s).
- **Random I/O**: 4 KiB random read latency (avg / p95) and throughput.
- **Drive health (best-effort)**:

  - PowerShell: `Get-Disk` + `Get-StorageReliabilityCounter` (if available).
  - `smartctl` (if **smartmontools** is installed and your USB bridge exposes SMART).

The script **only writes** inside `E:\HDD_Test\` (or your chosen drive) and deletes the file by default (use `--keep` to retain it).

---

## Requirements

- Windows 11
- Python **3.12** or **3.13**
- No external Python packages (uses standard library only)

**Optional (for SMART data):**

- smartmontools
  Install with one of:

  ```bat
  winget install smartmontools
  :: or
  choco install smartmontools
  ```

  Then reopen your terminal so `smartctl` is in `PATH`.

---

## Quick start

### Interactive mode (prompts for values)

Just run the script with no flags:

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py"
```

It will ask for:

- Drive letter (default `E:`)
- Test file size (GB)
- I/O chunk size (MB)
- Pattern (`random` or `zeros`)
- Random 4 KiB samples
- Keep file after run?
- Verify-only mode?
- Test file name

### Non-interactive (one-liner)

Example (2 GB write + verify on `E:`):

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --size-gb 2
```

See all options:

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" -h
```

---

## Common workflows

### 1) Standard sanity test (fast)

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --size-gb 2
```

- Confirms write/read integrity.
- Gives a ballpark of sequential speeds.

### 2) More realistic **read** numbers (avoid OS cache)

1. Create and **keep** a larger file:

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --size-gb 4 --keep
```

2. Unplug/replug the drive (or reboot), then **verify-only**:

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --verify-only
```

### 3) Max sequential write (cheap data generation)

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --size-gb 2 --pattern zeros
```

---

## Command reference

| Flag               | Description                                | Default        |
| ------------------ | ------------------------------------------ | -------------- |
| `-d`, `--drive`    | Target drive letter (e.g., `E:`)           | `E:`           |
| `--size-gb`        | Test file size in GB                       | `2.0`          |
| `--chunk-mb`       | I/O chunk size in MB                       | `64`           |
| `--pattern`        | Write pattern: `random` or `zeros`         | `random`       |
| `--random-samples` | Number of random 4 KiB read probes         | `400`          |
| `--keep`           | Keep the test file after run               | _off_          |
| `--verify-only`    | Skip writing; only read/hash existing file | _off_          |
| `--file-name`      | Test file name inside `HDD_Test` folder    | `testfile.bin` |
| `--no-interactive` | Force non-interactive even if no args      | _off_          |

---

## Interpreting results

- **Checksum**

  - ✅ _Match_ → integrity looks good.
  - ❌ _Mismatch_ → stop using the drive (return/replace).

- **Sequential speeds (MB/s)**

  - Typical USB 3.x external HDD: \~**100–200 MB/s** write/read.
  - Much lower may mean USB 2.0 port/cable, hub bottleneck, or background activity.

- **Random 4 KiB reads**

  - HDDs are inherently slow at small random reads (milliseconds).
  - If you see **\~0.01–0.05 ms** latency and **hundreds of MB/s**, that’s **OS cache**. Use the “realistic read” workflow above.

- **SMART / Reliability counters**

  - Ideal problematic attributes should be **0**:

    - Reallocated Sectors **(ID 5)**
    - Reported Uncorrectable **(ID 187)**
    - Current Pending **(ID 197)**
    - Offline Uncorrectable **(ID 198)**

  - **UDMA CRC Error Count (ID 199)** should be **0**—non-zero often means a bad USB cable/port.
  - `Get-StorageReliabilityCounter` may require **admin** and many USB bridges don’t expose it; that’s normal.

---

## Safety & cleanup

- Writes only to `X:\HDD_Test\<file>` (where `X:` is your chosen drive).
- By default, the test file is **deleted** at the end. Use `--keep` to retain it.
- Double-check your **drive letter** before running.

---

## Troubleshooting

- **“Not enough free space”** → reduce `--size-gb` or free space.
- **“smartctl not found”** → install smartmontools (see above), reopen terminal.
- **“Reliability counters not available”** → try an **elevated** PowerShell; some USB bridges simply don’t expose them.
- **Very slow writes** → confirm a USB 3.x port/cable; avoid hubs; try a different port/cable.
- **AV interference** → real-time antivirus can slow hashing/writes; temporarily exclude the `HDD_Test` folder if needed.

---

## Example output (trimmed)

```
=== External HDD Test v1.3.0 ===
[CONF ] Target drive: E: | size: 2.0 GB | chunk: 64 MB | pattern: random
[INFO ] E:\  Total: 465.76 GB  Free: 465.65 GB  Used: 110.70 MB
[PS   ] Get-Disk details:
Number : 1
...
[WRITE] Wrote 2.00 GB in 24.9s → 82.4 MB/s
[READ ] Read 2.00 GB in 3.9s  → 521.8 MB/s
[HASH ] Expected: <hash>
[HASH ]   Actual: <hash>
[OK   ] Read-after-write checksum matches ✅
[RAND ] 4KiB reads: avg 0.01 ms, p95 0.02 ms, ~401 MB/s over 400 samples
```

> Note: The very fast read/latency numbers above indicate **OS caching**. Use the verify-only workflow after a replug/reboot for realistic reads.

---

## Version

Script version: **1.3.0**
No external dependencies.
You can always see available flags with `-h`.

---
