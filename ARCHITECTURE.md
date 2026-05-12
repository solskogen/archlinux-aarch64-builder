# Arch Linux Multi-Architecture Builder — Architecture Specification

This document provides complete specifications to recreate the Arch Linux multi-architecture build system with identical functionality.

## System Overview

The system automatically maintains ports of Arch Linux for multiple architectures by:
1. Comparing package versions between x86_64 and target architecture repositories
2. Identifying outdated packages and missing dependencies
3. Building packages in correct dependency order using clean chroot environments
4. Detecting and resolving circular dependencies with two-stage builds
5. Propagating build failures to prevent linking against outdated dependencies
6. Uploading built packages to testing repositories
7. Reporting build status to DynamoDB with a live web dashboard

## Core Components

### 1. Main Scripts

#### `generate_build_list.py`
**Purpose**: Compare package versions and generate dependency-ordered build lists.

**Key Functions**:
- Downloads and parses Arch Linux package databases (core.db, extra.db, forge.db)
- Compares x86_64 vs target architecture package versions
- Filters out ARCH=any packages (don't need rebuilding) unless `--force` is used
- Fetches PKGBUILDs from git repositories with version tag checkout
- Handles package overrides from `package-overrides.json`
- Detects missing dependencies and adds them to the build list
- Sorts packages by dependency order using topological sort with Tarjan's cycle detection
- Outputs JSON build list with complete metadata

**Command Line Options**:
- `--packages PKG [PKG ...]`: Force rebuild specific packages
- `--preserve-order`: Skip dependency sorting, use exact command line order
- `--local`: Build from local PKGBUILDs only (use with --packages)
- `--aur PKG [PKG ...]`: Get specified packages from AUR
- `--blacklist FILE`: Skip packages matching patterns in file (default: blacklist.txt)
- `--rebuild-repo {core,extra}`: Rebuild all packages from repository
- `--single-stage`: Flatten to one build stage (no cycle duplication)
- `--target-testing`: Include target testing repos for comparison
- `--upstream-testing`: Include upstream testing repos for comparison
- `--force`: Force rebuild ARCH=any packages (use with --packages)
- `--no-update`: Skip git updates, use existing PKGBUILDs
- `--use-latest`: Use latest git commit instead of version tags (mutually exclusive with --no-update)
- `--no-check`: Exclude checkdepends from dependency resolution
- `--dry-run`: Show what would be generated without writing JSON or running git
- `--rsync`: Rsync x86_64 mirror before checking
- `-v, --verbose`: Show detailed progress messages
- `-q, --quiet`: Only show warnings and errors

**Git Repository Handling**:
- Official packages: Cloned via `pkgctl repo clone [--switch VERSION] PACKAGE`
- AUR packages: `git clone https://aur.archlinux.org/{name}.git`
- Override packages: Cloned from custom URL/branch in `package-overrides.json`
- Version tag checkout with fallback to latest commit if tag not found
- Stash/pop local changes across updates; exits on merge conflicts

**Version Comparison Logic**:
- Prefers `vercmp` (pacman's native tool) when available
- Handles epoch versions (1:2.0-1), git revisions (1.0+r123.gabcdef-1)
- Falls back to `packaging.version`, then string comparison
- Skips bootstrap-only packages (linux-api-headers, glibc, binutils, gcc) unless forced

**Output Format** (`packages_to_build.json`):
```json
{
  "_command": "./generate_build_list.py",
  "_timestamp": "2026-04-24T13:17:56.774556",
  "packages": [
    {
      "name": "package-name",
      "version": "1.0-2",
      "current_version": "1.0-1",
      "basename": "pkgbase-name",
      "repo": "core|extra",
      "depends": ["dep1", "dep2"],
      "makedepends": ["makedep1"],
      "checkdepends": ["checkdep1"],
      "provides": ["virtual-pkg"],
      "force_latest": false,
      "use_aur": false,
      "build_stage": 0,
      "cycle_group": null,
      "cycle_stage": null,
      "_sequence": 0
    }
  ]
}
```

Fields added during processing:
- `build_stage`: Integer depth in dependency tree (0 = no in-list deps)
- `cycle_group`: Integer cycle ID (null if not in a cycle)
- `cycle_stage`: 1 or 2 for cycle packages (null otherwise)
- `_sequence`: Topological sort order within a stage
- `added_reason`: Why a missing dependency was auto-added (e.g., "makedepends for vim")

#### `build_packages.py`
**Purpose**: Build packages in clean chroot environments.

**Command Line Options**:
- `--dry-run`: Show actions without executing
- `--json FILE`: JSON file with packages to build (default: packages_to_build.json)
- `--blacklist FILE`: Skip packages matching patterns (default: blacklist.txt)
- `--no-upload`: Build without uploading to repository
- `--cache DIR`: Custom pacman cache directory
- `--no-cache`: Clear cache before each build
- `--continue`: Continue from last successful package
- `--preserve-chroot`: Keep chroot after successful builds
- `--cleanup-on-failure`: Delete temporary chroots even on build failure
- `--stop-on-failure`: Stop on first failure
- `--keep-going`: Try building packages even if their dependencies failed (default: cascade-skip)
- `--chroot DIR`: Custom chroot directory
- `--parallel-jobs N`: Max packages to build in parallel (default: 1). Adaptive ramp-up: starts at 1, adds another when a build completes or every 20s if CPU idle ≥ 25%. Never exceeds this cap.
- `--no-reporting`: Skip DynamoDB build status updates
- `--no-check`: Skip installing checkdepends and running check()

**Build Process**:
1. Load packages from JSON, apply blacklist filtering
2. Mark packages as QUEUED in DynamoDB
3. Setup chroot environment (create with `mkarchroot` if needed)
4. Import GPG keys from `keys/pgp/` directory
5. Clean up old temporary chroots
6. For each package (dependency-ordered, parallel-capable):
   - Check if dependencies failed → skip if so
   - Handle cycle stage transitions (stage 1 → stage 2)
   - Create temporary chroot via `rsync` from root chroot
   - Update package database with `arch-nspawn`
   - Install checkdepends (makechrootpkg handles depends/makedepends)
   - Check PKGBUILD arch array → only use `--ignorearch` when needed
   - Build with `makechrootpkg -l temp-{pkg}-{timestamp}`
   - Stream output to console and log file simultaneously
   - Upload to `{repo}-testing` (or `forge`) via `repo-upload`
   - Clear built packages from cache
   - Clean up temporary chroot
   - Report status to DynamoDB with CPU/memory metrics
7. Save failed packages to `failed_packages.json`

**Parallel Building**:
- Uses `ThreadPoolExecutor` with dependency-ready queue
- Each package starts as soon as its dependencies complete
- Adaptive ramp-up: starts at 1 job; launches another when a build completes, or every 20s if CPU idle ≥ 25%; never exceeds `--parallel-jobs`
- Cascade-skips dependents when a package fails (unless `--keep-going`)
- Within-SCC checkdepends-only edges are dropped to break cycles that aren't actual build-order constraints
- Mutex groups prevent conflicting packages from building concurrently (e.g., firefox variants)

**Chroot Management**:
- Default chroot: configured via `build_root` in config.ini
- Default cache: configured via `cache_path` in config.ini
- Temporary chroots: `temp-{package}-{YYYYMMDDHHmmSS}`
- Automatic cleanup on SIGINT/SIGTERM

#### `bootstrap_toolchain.py`
**Purpose**: Build core toolchain packages in a staged approach.

**Staged Build Process**:
- Stage 1: linux-api-headers, glibc, binutils, gcc, gmp, mpfr, libmpc, libisl
- Stage 2: glibc, binutils, gcc, gmp, mpfr, libmpc, libisl, libtool, valgrind

**Special Handling**:
- gcc is cloned from a custom repository (`gitlab.archlinux.org/solskogen/gcc.git`, branch `experimental`)
- Lock file (`bootstrap.lock`) prevents concurrent runs with PID-based stale detection
- Clears pacman cache between builds to force using newly uploaded packages
- Installs all toolchain packages + gcc-libs before each build

**Command Line Options**:
- `--chroot DIR`: Custom chroot path
- `--cache DIR`: Custom cache directory
- `--dry-run`: Show actions without executing
- `--continue`: Continue from last successful package
- `--start-from PACKAGE`: Start from specific package
- `--one-shot PACKAGE`: Build only the specified package once

#### `repo_analyze.py`
**Purpose**: Analyze repository differences and inconsistencies.

**Analysis Types**:
- Package name mismatches (split packages differing between architectures)
- Outdated/missing ARCH=any packages
- Repository inconsistencies (packages in wrong repo, cross-repo duplicates)
- Version differences (target newer than x86_64)
- Target-only packages (with -bin version comparison)
- Orphaned split packages (removed upstream but still in target)
- Broken dependencies (target packages depending on something not available)
- Unsatisfied version constraints (includes SONAME drift — e.g. a package links to libfoo.so=5 but target provides libfoo.so=6)
- Missing packages blocked by blacklisted dependencies (with dep chain)

**Command Line Options**:
- `--blacklist FILE`: Skip blacklisted packages
- `--no-blacklist`: Ignore blacklist entirely
- `--use-existing-db`: Use existing database files instead of downloading
- `--missing-pkgbase`: Print missing package base names (space delimited)
- `--outdated-any`: Show outdated ARCH=any packages
- `--missing-any`: Show missing ARCH=any packages
- `--repo-issues`: Show repository inconsistencies and duplicates
- `--target-newer`: Show packages where target architecture is newer
- `--target-only`: Show target architecture only packages
- `--orphaned`: Show orphaned split packages (removed upstream)
- `--broken-deps`: Show target packages with unresolvable dependencies
- `--unsatisfied-deps`: Show target packages with unsatisfied version constraints (SONAME drift)
- `--blocked-by-blacklist`: Show missing packages blocked by blacklisted deps
- `--target-only-files`: Print filenames of target-only packages in core/extra

#### `find_dependents.py`
**Purpose**: Query package dependency relationships in both directions.

**Usage**:
```bash
./find_dependents.py gcc              # What depends on gcc?
./find_dependents.py -f gcc           # What does gcc depend on?
./find_dependents.py --depends-only gcc    # Runtime deps only
./find_dependents.py --makedepends-only gcc  # Build deps only
```

**Output**: Space-separated list of package basenames.

#### `auto_builder.py`
**Purpose**: Continuous build daemon that automates the full build cycle.

**Cycle Flow**:
1. Update heartbeat in DynamoDB
2. Load and prune failure tracker (`auto_builder_failures.json`)
3. Sync ARCH=any packages from upstream (`sync_any_packages.py`)
4. Generate build list (`generate_build_list.py`)
5. Filter out previously failed packages (version-aware, skip after 2 failures)
6. Start background reporter thread (syncs DynamoDB every 30s)
7. Build packages (`build_packages.py --continue --parallel-jobs 5`)
8. Stop background reporter
9. Ingest build logs (`generate_report.py`)
10. Promote packages from testing to stable repos
11. Clean up successful build logs
12. Record new failures, update DynamoDB (RETRY for first failure, FAILED for second)

**Command Line Options**:
- `--interval N`: Seconds between cycles (default: 180)
- `--once`: Run one cycle and exit
- `--blacklist FILE`: Blacklist file (default: blacklist.txt)
- `-q, --quiet`: Quiet mode for generate_build_list
- `--generate-args ...`: Extra args passed to generate_build_list.py

#### `sync_any_packages.py`
**Purpose**: Sync ARCH=any packages from x86_64 upstream to target testing repos.

**Process**:
1. Rsync x86_64 mirror to local path
2. Parse x86_64 .db files for ARCH=any packages
3. Download target architecture .db files (stable + testing)
4. Compare versions, copy missing/outdated packages to testing repos

**Command Line Options**:
- `--dry-run`: Show what would be synced
- `--no-rsync`: Skip rsync, use existing mirror data

#### `generate_report.py`
**Purpose**: Calculate repo stats, reconcile DynamoDB state, and publish to `ArchBuilder-RepoStats`.

Parses local `.db` files (in parallel) to count packages per repo, calculates outdated ARCH=any counts, checks which packages are in testing repos, and writes all stats to the `ArchBuilder-RepoStats` DynamoDB table.

Also reconciles drift between `ArchBuilder-Latest` and `ArchBuilder-Builds` when `build_packages.py` isn't running:
- Revives stale QUEUED/BUILDING entries in Latest back to their true last terminal status
- Deletes stale QUEUED/BUILDING rows from Builds (from aborted builds)
- Reverts stale RETRY entries when no retry is pending in `auto_builder_failures.json`
- General drift fix: for every Latest entry, compares against the true newest Builds entry in parallel (32-way), correcting any mismatch

**Command Line Options**:
- `-v`, `--verbose`: Show progress and timing for each step

#### `dynamo_reporter.py`
**Purpose**: DynamoDB/S3 reporting module used by build scripts.

**DynamoDB Tables**:
- `ArchBuilder-Builds` — PK: PkgName, SK: BuildId (`{timestamp}#{version}`)
- `ArchBuilder-Latest` — PK: PkgName, stores latest status
- `ArchBuilder-RepoStats` — PK: key, key-value store for dashboard metadata

**S3**: Build logs uploaded as gzip to `s3://{bucket}/arch/reports/logs/{package}/{timestamp}.log.gz`

**Key Functions**:
- `mark_queued(packages)`: Batch-write QUEUED status, returns build ID mapping
- `mark_building(package, build_id)`: Update to BUILDING with timestamp
- `update_build_status(...)`: Write/update build record with metrics
- `upload_build_log(package, build_id, log_path)`: Upload gzip'd log to S3
- `LiveLogUploader`: Context manager that periodically uploads log to S3 during build
- `sync_repo_stats()`: Write heartbeat, load average, memory stats
- `mark_aborted()`: Mark all QUEUED/BUILDING items as ABORTED

### 2. Utility Module (`utils.py`)

**Package Validation**:
- `validate_package_name(name)`: Regex `^[a-zA-Z0-9][a-zA-Z0-9+._-]*$`
- `safe_path_join(base, user_input)`: Prevents directory traversal attacks

**Version Comparison** (`ArchVersionComparator`):
- `compare(v1, v2)`: Returns -1/0/1, prefers `vercmp` when available
- `is_newer(current, target)`: Returns True if target is newer
- Handles epochs, git revisions (+r), pkgrel, fallback to string comparison

**Database Operations**:
- `parse_database_file(db_file, include_any=False)`: Parse pacman .db tarball
- `load_database_packages(urls, arch_suffix, download, include_any)`: Parallel download and parse
- `load_x86_64_packages(...)`: Load x86_64 packages from mirror
- `load_target_arch_packages(...)`: Load target arch packages from configured repos
- `load_all_packages_parallel(...)`: Load both architectures in parallel
- `load_packages_unified(...)`: Unified loading function for all scripts

**PKGBUILD Processing**:
- `parse_pkgbuild_deps(pkgbuild_path)`: Extract depends/makedepends/checkdepends via bash sourcing

**Blacklist Management**:
- `load_blacklist(file)`: Load patterns with comment/empty line filtering
- `filter_blacklisted_packages(packages, blacklist)`: Filter using fnmatch wildcards

**Build Utilities** (`BuildUtils` class):
- `run_command(cmd, ...)`: Execute with dry-run support
- `setup_chroot(chroot_path, cache_path)`: Create/setup build environment with mkarchroot
- `cleanup_old_logs(package_name, keep_count=3)`: Log rotation
- `clear_packages_from_cache(cache_path, pkg_names)`: Remove specific packages from cache

**Standalone Functions**:
- `upload_packages(pkg_dir, target_repo, dry_run)`: Upload via `repo-upload` to S3
- `import_gpg_keys()`: Import keys from `keys/pgp/` directory
- `get_target_architecture()`: Read CARCH from `chroot-config/makepkg.conf`
- `find_missing_dependencies(packages, x86_packages, target_packages)`: Recursive missing dep detection
- `compare_bin_package_versions(provided, x86)`: Compare -bin package versions ignoring pkgrel
- `check_auto_builder_lock(script_name)`: Exit if auto_builder.py is running

### 3. Test Suite (`test_all.py`)

88 tests across 33 test classes covering security, version comparison, dependency resolution, blacklist patterns, build utilities, CLI interfaces, edge cases, and integration. Works with or without pytest.

## Data Flow

### Package Discovery Flow
```
x86_64 .db files ──┐
                    ├──▶ Compare versions ──▶ Apply blacklist ──▶ Fetch PKGBUILDs
target .db files ───┘                                                    │
                                                                         ▼
                                              Find missing deps ◀── Parse dependencies
                                                    │
                                                    ▼
                                            Topological sort (Tarjan's for cycles)
                                                    │
                                                    ▼
                                            packages_to_build.json
```

### Build Flow
```
packages_to_build.json ──▶ Apply blacklist ──▶ Mark QUEUED in DynamoDB
                                                       │
                                                       ▼
                                              Setup chroot environment
                                                       │
                                              ┌────────┴────────┐
                                              ▼                 ▼
                                        Sequential        Parallel (ThreadPool)
                                              │                 │
                                              └────────┬────────┘
                                                       ▼
                                              For each package:
                                              1. Check failed deps → skip
                                              2. Create temp chroot (rsync)
                                              3. Install checkdepends
                                              4. makechrootpkg build
                                              5. Upload to testing repo
                                              6. Clear cache
                                              7. Report to DynamoDB
                                              8. Cleanup temp chroot
```

## Key Algorithms

### Topological Sort with Cycle Detection
1. Build dependency graph from package metadata
2. Resolve provides relationships for virtual dependencies
3. Add transitive dependencies through non-build-list packages
4. Find strongly connected components using Tarjan's algorithm
5. Sort cycles by external dependency count (most depended-upon first)
6. Build external dependencies of cycles first
7. Build cycle packages in two stages (stage 1 initial, stage 2 final)
8. Build remaining packages in topological order
9. Assign `build_stage`, `cycle_group`, `cycle_stage`, `_sequence` metadata

### Version Comparison
1. Try `vercmp` (pacman's native tool) first
2. Split epoch from version string, compare epochs numerically
3. Detect git revision patterns (+r123)
4. For git versions: compare base version, then revision number
5. Use `packaging.version` for standard versions
6. Fallback to dot-split numeric comparison, then string comparison

### Dependency Failure Propagation
1. Build provides mapping from all packages in build list
2. Before building each package, check all deps against failed set
3. Resolve through provides (if dep X is provided by failed Y, skip)
4. For cycle packages: skip stage 2 if stage 1 failed

## Security

- **Package name validation**: Regex `^[a-zA-Z0-9][a-zA-Z0-9+._-]*$`
- **Path traversal prevention**: `safe_path_join()` validates and resolves paths
- **Chroot isolation**: Each package builds in isolated temporary chroot
- **Privilege management**: Sudo only for specific operations (rsync, rm, arch-nspawn, makechrootpkg)

## Configuration Requirements

### Required Tools
- `makechrootpkg`, `pkgctl`, `arch-nspawn` (from devtools)
- `repo-upload` (custom S3 upload tool)
- `git`, `rsync`, `wget`
- `python3` with `packaging` library
- AWS credentials for DynamoDB/S3 (via boto3)

### Required Files
- `config.ini` — Build paths and settings
- `chroot-config/pacman.conf` — Pacman configuration for chroot
- `chroot-config/makepkg.conf` — Build settings (must contain `CARCH=`)
- `blacklist.txt` — Package patterns to skip (optional)
- `package-overrides.json` — Custom git repos (optional)
