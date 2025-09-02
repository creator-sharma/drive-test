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

  - PowerShell: `Get-Disk` + `Get-StorageReliabilityCounter` (if available; often needs Admin).
  - SMART via `smartctl` (from **smartmontools**). The script auto-detects `smartctl` and, for USB enclosures, automatically retries with `-d sat` and `-d scsi`.

- **Run logging**: JSON Lines via `--report-json <path>`; easy to append & analyze later.

The script **only writes** inside `E:\HDD_Test\` (or your chosen drive) and deletes the file by default (use `--keep` to retain it).

---

## Requirements

- Windows 11
- Python **3.12** or **3.13**
- No external Python packages (standard library only)

**Optional (for SMART data):**

- **smartmontools**

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

Run with no flags:

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py"
```

You’ll be prompted for:

- Drive letter (default `E:`)
- Test file size (GB)
- I/O chunk size (MB)
- Pattern (`random` or `zeros`)
- Random 4 KiB samples
- Keep file after run?
- Verify-only mode?
- **(Verify-only)** Run random 4 KiB probe **before** sequential read? (recommended)
- Test file name
- Path to `smartctl.exe` (press Enter for auto-detect)
- **Save results to JSON** (path or `y` for default)

> If you answer `y` at the JSON prompt, logs are saved to
> `…\hdd_runs_<DRIVE>.jsonl` in the script folder (e.g., `hdd_runs_E.jsonl`).

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

## Logging & history (JSONL)

Use `--report-json` to append one JSON object per run (newline-delimited):

- Default file in script folder:

  ```bat
  py -3.13 "...hdd_test.py" --report-json y
  ```

  Saves to `...\hdd_runs_E.jsonl` (drive letter included in the name).

- Custom folder or file:

  ```bat
  :: folder -> uses default file name inside it
  py -3.13 "...hdd_test.py" --report-json "C:\logs"
  :: explicit file
  py -3.13 "...hdd_test.py" --report-json "C:\logs\my_runs.jsonl"
  ```

**Read JSONL quickly**

PowerShell (last run, pretty table):

```powershell
$last = Get-Content "C:\Dev\Project_25\_scripts\drive-test\hdd_runs_E.jsonl" -Tail 1 | ConvertFrom-Json
$last | Select-Object timestamp_local, drive, mode,
  @{n='read_MBps';e={$_.read.mb_per_s}},
  @{n='rand_avg_ms';e={$_.random_read.avg_ms}},
  @{n='smart_overall';e={$_.smart.overall}} | Format-Table -Auto
```

Convert JSONL → one JSON array:

```powershell
(Get-Content "...\hdd_runs_E.jsonl" | ForEach-Object { $_ | ConvertFrom-Json }) |
  ConvertTo-Json -Depth 6 | Out-File "...\hdd_runs_E.json"
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

**Recommended:** keep a file, **replug**, then verify-only with random probe first.

```bat
:: Create and keep a larger file
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --size-gb 4 --keep

:: Safely eject + unplug/replug the drive (or reboot), then:
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --verify-only --random-first --report-json y
```

### 3) Max sequential write (cheap data generation)

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --drive E: --size-gb 2 --pattern zeros
```

### 4) If `smartctl` isn’t on PATH

```bat
py -3.13 "C:\Dev\Project_25\_scripts\drive-test\hdd_test.py" --smartctl "C:\Program Files\smartmontools\bin\smartctl.exe"
```

(USB bridges are auto-tried with `-d sat` and `-d scsi`.)

---

## Command reference

| Flag               | Description                                                                      | Default        |
| ------------------ | -------------------------------------------------------------------------------- | -------------- |
| `-d`, `--drive`    | Target drive letter (e.g., `E:`)                                                 | `E:`           |
| `--size-gb`        | Test file size in GB                                                             | `2.0`          |
| `--chunk-mb`       | I/O chunk size in MB                                                             | `64`           |
| `--pattern`        | Write pattern: `random` or `zeros`                                               | `random`       |
| `--random-samples` | Number of random 4 KiB read probes                                               | `400`          |
| `--keep`           | Keep the test file after run                                                     | off            |
| `--verify-only`    | Skip writing; only read/hash existing file                                       | off            |
| `--random-first`   | _(Verify-only)_ run random probe **before** the sequential read (more realistic) | off†           |
| `--file-name`      | Test file name inside `HDD_Test` folder                                          | `testfile.bin` |
| `--smartctl`       | Path to `smartctl.exe` (auto-detected if omitted)                                | —              |
| `--report-json`    | Append JSON Lines to file (e.g., `y`, a folder, or an explicit `.jsonl`)         | off            |
| `--no-interactive` | Force non-interactive even with no args                                          | off            |

† When you use **verify-only**, the script defaults `--random-first` to **on**, unless you explicitly set it otherwise.

---

## Interpreting results

- **Checksum**

  - ✅ _Match_ → integrity looks good.
  - ❌ _Mismatch_ → stop using the drive (return/replace).

- **Sequential speeds (MB/s)**

  - Typical USB 3.x external HDD: **\~100–200 MB/s** write/read.
  - Much lower may mean USB 2.0 port/cable, hub bottleneck, or background activity.

- **Random 4 KiB reads**

  - HDDs are inherently slow at small random reads (milliseconds).
  - **Uncached** HDD numbers often look like **\~8–15 ms avg**, **\~12–25 ms p95**, low MB/s (\~0.3–0.6).
  - If you see **\~0.01–0.05 ms** latency and **hundreds of MB/s**, that’s **OS cache**. Use the “realistic read” workflow above.

- **SMART / Reliability counters**

  - Attributes that should be **0**:

    - Reallocated Sectors **(ID 5)**
    - Reported Uncorrectable **(ID 187)**
    - Current Pending **(ID 197)**
    - Offline Uncorrectable **(ID 198)**

  - **UDMA CRC Error Count (ID 199)** should be **0**—non-zero often means a bad USB cable/port.
  - `Get-StorageReliabilityCounter` may require **Admin**, and many USB bridges don’t expose it (normal).

---

## Safety & cleanup

- Writes only to `X:\HDD_Test\<file>` (where `X:` is your chosen drive).
- By default, the test file is **deleted** at the end. Use `--keep` to retain it.
- Double-check your **drive letter** before running.

---

## Troubleshooting

- **“Not enough free space”** → reduce `--size-gb` or free space.
- **“smartctl not found”** → install smartmontools (see above), reopen terminal, or pass `--smartctl "C:\...\smartctl.exe"`.
- **“Reliability counters not available”** → try an **elevated** PowerShell; many USB bridges simply don’t expose them.
- **Very slow writes** → confirm a USB 3.x port/cable; avoid hubs; try a different port/cable.
- **AV interference** → real-time antivirus can slow hashing/writes; temporarily exclude the `HDD_Test` folder if needed.

---

## Example output (trimmed)

```
=== External HDD Test v1.5.0 ===
[CONF ] Target drive: E: | size: 2.0 GB | chunk: 64 MB | pattern: random
[INFO ] E:\  Total: 465.76 GB  Free: 463.65 GB  Used: 2.11 GB
[PS   ] Get-Disk details:
Number       : 1
FriendlyName : Innostor Ext. HDD
Model        : Ext. HDD
BusType      : USB
HealthStatus : Healthy
Size         : 500107862016
[SMART] smartctl on E: (key lines):
SMART overall-health self-assessment test result: PASSED
5 Reallocated_Sector_Ct ... 0
197 Current_Pending_Sector ... 0
198 Offline_Uncorrectable ... 0
199 UDMA_CRC_Error_Count ... 0
[MODE ] Verify-only: will hash existing file.
[RAND ] 4KiB reads (pre-read): avg 10.1 ms, p95 15.2 ms, ~0.39 MB/s over 400 samples
[READ ] Reading & hashing 2.00 GB from E:\HDD_Test\testfile.bin
[READ ] Read 2.00 GB in 21.1s  →  97.1 MB/s
[REPORT] Appended JSON to ...\hdd_runs_E.jsonl
```

> Note: Running the **random probe before** the big sequential read keeps results uncached and HDD-realistic.

---

## Version

Script version: **1.5.0**
No external dependencies.
See available flags with `-h`.

---

## (Optional) SMART self-tests

You can trigger on-drive diagnostics (the script doesn’t run these automatically):

```powershell
# Most USB enclosures: use -d sat
smartctl -t short -d sat \\.\PhysicalDrive1
# Later, view results:
smartctl -l selftest -d sat \\.\PhysicalDrive1

# Long/offline test (takes hours on HDDs)
smartctl -t long -d sat \\.\PhysicalDrive1
```

---
