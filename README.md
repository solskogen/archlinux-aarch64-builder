# Arch Linux Multi-Architecture Builder

An automated build system that maintains ports of Arch Linux for multiple architectures (AArch64, RISC-V, s390x, etc.) by comparing package versions between x86_64 and target architectures, then building outdated packages in correct dependency order.

## Quick Start

```bash
# 1. Generate list of packages that need building
./generate_build_list.py

# 2. Build the packages
./build_packages.py
```

### Continuous Auto Builder

For automated continuous building:

```bash
# Start the auto builder daemon (checks every 180s)
./auto_builder.py

# Run once and exit
./auto_builder.py --once

# Custom interval
./auto_builder.py --interval 300
```

The auto builder:
- Syncs ARCH=any packages from upstream x86_64 mirror
- Generates build lists and builds outdated packages
- Tracks failed packages (won't retry same version twice)
- Promotes packages from testing to stable repos
- Reports build status to DynamoDB (live web dashboard at `reports/latest.html`)
- Shows real-time build status (QUEUED → BUILDING → SUCCESS/FAILED)

## Prerequisites

### Required Tools
```bash
sudo pacman -S devtools git rsync python-packaging
```

### Configuration Files
1. **`config.ini`** — Main configuration (see [Configuration](#configuration))
2. **`chroot-config/pacman.conf`** — Pacman configuration for build chroot
3. **`chroot-config/makepkg.conf`** — Build settings (must contain `CARCH=your_target_arch`)
4. **`package-overrides.json`** — Optional: Custom git repositories for specific packages

### System Requirements
- **Disk Space**: 50GB+ free space for chroot and cache
- **Memory**: 8GB+ recommended
- **Network**: Stable connection for downloads and uploads
- **Permissions**: Passwordless sudo for chroot management

## Key Concepts

**Target Architecture**: The architecture you're building packages for (e.g., aarch64), read from `chroot-config/makepkg.conf`.

**Upstream Architecture**: The reference architecture to compare against (x86_64).

**Chroot Environment**: Isolated build environment that ensures clean, reproducible builds.

**Testing Repositories**: Where built packages are uploaded first (core-testing, extra-testing) before promotion to stable repositories.

## How Build Stages Work

The build system uses topological sorting to order packages by their dependencies — packages with no dependencies on other packages in the build list go in stage 0, packages that only depend on stage 0 packages go in stage 1, and so on. When circular dependencies are detected using Tarjan's algorithm (e.g., package A depends on B, B depends on A), those packages are built twice: first in an early stage to satisfy initial dependencies, then again in a later stage to link against the complete versions. Packages within the same stage can be built in parallel since they don't depend on each other.

## Basic Workflow

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Compare Package │───▶│ Generate Build   │───▶│ Build Packages  │
│ Versions        │    │ List (JSON)      │    │ in Chroot       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         │
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Promotion to    │◀───│ Upload to Testing│◀───│ Built .pkg.tar. │
│ Stable Repos    │    │ via repo-upload  │    │ zst Files       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Configuration

Create `config.ini` with your settings:

```ini
[build]
build_root = /scratch/builder
cache_path = /scratch/builder/pacman-cache
upload_bucket = your-s3-bucket.example.com
target_base_url = https://your-repo.com/arch
x86_64_mirror = https://geo.mirror.pkgbuild.com

[paths]
mirror_path = /scratch/archlinux
repos_path = /mnt/repos
move_to_release_script = /mnt/repos/move-from-testing-to-release.sh
```

**Configuration Options:**
- `build_root`: Directory for build operations and chroot (default: `/scratch/builder`)
- `cache_path`: Pacman package cache directory (default: `{build_root}/pacman-cache`)
- `upload_bucket`: S3 bucket name for uploading built packages via `repo-upload`
- `target_base_url`: Base URL for your target architecture repositories
- `x86_64_mirror`: URL for x86_64 package mirror (default: `https://geo.mirror.pkgbuild.com`)
- `mirror_path`: Local rsync mirror of x86_64 packages (used by `sync_any_packages.py`)
- `repos_path`: Where testing and stable repository directories live
- `move_to_release_script`: Script to promote packages from testing to stable

## Package Overrides

For packages that need custom git repositories or branches, create `package-overrides.json`:

```json
{
  "gcc": {
    "url": "https://gitlab.archlinux.org/solskogen/gcc.git",
    "branch": "experimental"
  },
  "linux": {
    "url": "https://gitlab.archlinux.org/bschnei/linux.git",
    "branch": "aarch64"
  }
}
```

When overrides are specified, the system clones from the custom URL and checks out the specified branch instead of using `pkgctl repo clone` with version tags.

## Common Usage Examples

### Find and Build Outdated Packages
```bash
./generate_build_list.py
./build_packages.py
```

### Build Specific Packages
```bash
./generate_build_list.py --packages vim firefox gcc
./build_packages.py
```

### Build from AUR
```bash
./generate_build_list.py --aur yay paru
./build_packages.py
```

### Build from Local PKGBUILDs
```bash
./generate_build_list.py --local --packages my-package
./build_packages.py
```

### Analyze Repository Differences
```bash
./repo_analyze.py                    # Show all differences
./repo_analyze.py --missing-pkgbase  # List missing package bases
./repo_analyze.py --outdated-any     # Show outdated ARCH=any packages
./repo_analyze.py --target-only      # Show target-only packages
./repo_analyze.py --orphaned         # Show orphaned split packages
./repo_analyze.py --broken-deps      # Show target packages with unresolvable dependencies
./repo_analyze.py --unsatisfied-deps # Show unsatisfied version constraints (SONAME drift)
./repo_analyze.py --blocked-by-blacklist  # Show missing packages blocked by blacklisted deps
```

### Dry Run
```bash
./generate_build_list.py --packages vim gcc
./build_packages.py --dry-run
```

### Bootstrap Toolchain
```bash
./bootstrap_toolchain.py             # Full 2-stage bootstrap
./bootstrap_toolchain.py --one-shot gcc  # Build single toolchain package
```

## Where Built Packages End Up

1. **Local Files**: Built `.pkg.tar.zst` files are created in `./pkgbuilds/{package}/`
2. **Testing Upload**: Packages are uploaded via `repo-upload` to S3:
   - Core packages → `core-testing`
   - Extra packages → `extra-testing`
   - Other packages → `forge`
3. **Promotion**: Packages are moved from testing to stable repos (auto_builder does this automatically)

## Scripts

| Script | Description |
|--------|-------------|
| `generate_build_list.py` | Compare versions, generate dependency-ordered build list |
| `build_packages.py` | Build packages in clean chroot environments |
| `bootstrap_toolchain.py` | 2-stage bootstrap build for gcc/glibc/binutils toolchain |
| `auto_builder.py` | Continuous build daemon (generate → build → promote cycle) |
| `repo_analyze.py` | Analyze differences between x86_64 and target repos |
| `find_dependents.py` | Query package dependency relationships |
| `sync_any_packages.py` | Sync ARCH=any packages from x86_64 to target testing repos |
| `generate_report.py` | Calculate repo stats and publish to DynamoDB |
| `dynamo_reporter.py` | DynamoDB/S3 reporting module (build status, logs, stats) |
| `utils.py` | Shared utilities (validation, version comparison, DB parsing, etc.) |
| `test_all.py` | Comprehensive test suite (88 tests) |

## Command Line Options

### generate_build_list.py

| Option | Description |
|--------|-------------|
| `--packages PKG [PKG ...]` | Force rebuild specific packages by name |
| `--preserve-order` | Preserve exact order specified in --packages (skip dependency sorting) |
| `--local` | Build packages from local PKGBUILDs only (use with --packages) |
| `--aur PKG [PKG ...]` | Get specified packages from AUR |
| `--blacklist FILE` | File containing packages to skip (default: blacklist.txt) |
| `--rebuild-repo {core,extra}` | Rebuild all packages from specified repository |
| `--single-stage` | Flatten to one build stage (no cycle duplication) |
| `--target-testing` | Include target testing repos for comparison |
| `--upstream-testing` | Include upstream testing repos for comparison |
| `--force` | Force rebuild ARCH=any packages (use with --packages) |
| `--no-update` | Skip git updates, use existing PKGBUILDs |
| `--use-latest` | Use latest git commit instead of version tag (mutually exclusive with --no-update) |
| `--no-check` | Exclude checkdepends from dependency resolution |
| `--dry-run` | Show what would be generated without writing JSON or running git |
| `--rsync` | Rsync x86_64 mirror before checking for packages |
| `-v, --verbose` | Show detailed progress messages |
| `-q, --quiet` | Only show warnings and errors |

### build_packages.py

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without executing |
| `--json FILE` | JSON file with packages to build (default: packages_to_build.json) |
| `--blacklist FILE` | File containing packages to skip (default: blacklist.txt) |
| `--no-upload` | Build packages but don't upload to repository |
| `--cache DIR` | Custom pacman cache directory |
| `--no-cache` | Clear cache before each package build |
| `--continue` | Continue from last successful package |
| `--preserve-chroot` | Preserve chroot even on successful builds |
| `--cleanup-on-failure` | Delete temporary chroots even on build failure |
| `--stop-on-failure` | Stop building on first package failure |
| `--keep-going` | Try building packages even if their dependencies failed |
| `--chroot DIR` | Custom chroot directory path |
| `--parallel-jobs N` | Max packages to build in parallel (default: 1). Adaptive ramp-up: starts at 1, adds another every 20s if CPU idle ≥ 25% |
| `--no-reporting` | Skip updating the DynamoDB build report |
| `--no-check` | Skip installing checkdepends and running check() |

### bootstrap_toolchain.py

| Option | Description |
|--------|-------------|
| `--chroot DIR` | Chroot path (default: from config.ini) |
| `--cache DIR` | Pacman cache directory (default: from config.ini) |
| `--dry-run` | Show what would be done without building |
| `--continue` | Continue from last successful package |
| `--start-from PKG` | Start from specific package in the build order |
| `--one-shot PKG` | Build only the specified package once |

### repo_analyze.py

| Option | Description |
|--------|-------------|
| `--blacklist FILE` | Blacklist file (default: blacklist.txt) |
| `--no-blacklist` | Ignore blacklist |
| `--use-existing-db` | Use existing database files instead of downloading |
| `--missing-pkgbase` | Print missing pkgbase names (space delimited) |
| `--outdated-any` | Show outdated ARCH=any packages |
| `--missing-any` | Show missing ARCH=any packages |
| `--repo-issues` | Show repository inconsistencies and duplicates |
| `--target-newer` | Show packages where target architecture is newer |
| `--target-only` | Show target architecture only packages |
| `--orphaned` | Show orphaned split packages (removed upstream) |
| `--broken-deps` | Show target packages with unresolvable dependencies |
| `--unsatisfied-deps` | Show target packages with unsatisfied version constraints (includes SONAME drift) |
| `--blocked-by-blacklist` | Show missing packages blocked by blacklisted dependencies |
| `--target-only-files` | Print filenames of target-only packages in core/extra |

### find_dependents.py

| Option | Description |
|--------|-------------|
| `PACKAGE` | Package name to query (positional) |
| `-f, --forward` | Show dependencies OF this package (default: show dependents) |
| `--depends-only` | Runtime dependencies only |
| `--makedepends-only` | Build dependencies only |

## Build Reporting

Build status is reported to AWS DynamoDB tables and S3:
- **ArchBuilder-Builds** — Per-package build records (status, duration, version, CPU/memory stats)
- **ArchBuilder-Latest** — Latest status per package (for fast lookups)
- **ArchBuilder-RepoStats** — Key-value store (package counts, heartbeat, load, memory)
- **S3 logs** — Build logs uploaded as gzip to `s3://{bucket}/arch/reports/logs/{package}/`

The web dashboard (`reports/latest.html`) fetches data from an API Gateway endpoint backed by these DynamoDB tables.

## Troubleshooting

### Common Issues

**"Command not found" errors**
```bash
sudo pacman -S devtools git rsync python-packaging
```

**"Permission denied" errors**
```bash
# Set up passwordless sudo for required commands
sudo visudo
# Add: %wheel ALL=(ALL) NOPASSWD: /usr/bin/rm, /usr/bin/rsync, /usr/bin/arch-nspawn, /usr/bin/makechrootpkg
```

**"No packages to build" message**
```bash
./generate_build_list.py --packages your-package
./generate_build_list.py --target-testing --upstream-testing
```

**Build failures**
```bash
ls logs/                                          # Check build logs
./build_packages.py --continue                    # Resume from last success
./build_packages.py --preserve-chroot --stop-on-failure  # Debug in chroot
```

**Network/download issues**
```bash
./generate_build_list.py --no-update              # Use existing PKGBUILDs
```

## License

This project is licensed under the BSD Zero Clause License — see the [LICENSE](LICENSE) file for details.
