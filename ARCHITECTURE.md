# Arch Linux Multi-Architecture Builder - Complete Architecture Specification

This document provides complete specifications to recreate the Arch Linux multi-architecture build system with identical functionality.

## System Overview

The system automatically maintains ports of Arch Linux for multiple architectures by:
1. Comparing package versions between x86_64 and target architecture repositories
2. Identifying outdated packages and missing dependencies
3. Building packages in correct dependency order using clean chroot environments
4. **Detecting and resolving circular dependencies with two-stage builds**
5. **Propagating build failures to prevent linking against outdated dependencies**
6. Uploading built packages to testing repositories

## Core Components

### 1. Main Scripts

#### `generate_build_list.py`
**Purpose**: Compare package versions and generate dependency-ordered build lists

**Key Functions**:
- Downloads and parses Arch Linux package databases (core.db, extra.db)
- Compares x86_64 vs target architecture package versions using Arch Linux state repository
- Filters out ARCH=any packages (don't need rebuilding)
- Extracts complete package metadata including dependencies
- Fetches PKGBUILDs from git repositories with version tag checkout
- Sorts packages by dependency order using topological sort
- Outputs JSON build list with metadata

**Command Line Options**:
- `--packages PKG [PKG ...]`: Force rebuild specific packages
- `--preserve-order`: Skip dependency sorting, use exact command line order
- `--local`: Build from local PKGBUILDs only
- `--aur PKG [PKG ...]`: Get specified packages from AUR (implies --packages mode)
- `--blacklist FILE`: Skip packages matching patterns in file
- `--missing-packages`: List packages missing from target architecture
- `--rebuild-repo {core,extra}`: Rebuild all packages from repository
- `--no-update`: Skip git updates, use existing PKGBUILDs
- `--use-latest`: Use latest git commit instead of version tags
- `--target-testing`: Include target testing repos for comparison
- `--upstream-testing`: Include upstream testing repos for comparison
- `--force`: Force rebuild ARCH=any packages (use with --packages)
- Mutually exclusive: `--no-update` and `--use-latest`

**Git Repository Handling**:
- Official packages: Clone from `https://gitlab.archlinux.org/archlinux/packaging/packages/{name}.git`
- AUR packages: Clone from `https://aur.archlinux.org/{name}.git`
- Handle `++` in package names by converting to `plusplus` for GitLab URLs
- Checkout specific version tags when available, fallback to latest commit
- Use `git pull` for updates (handles any default branch automatically)

**Version Comparison Logic**:
- Uses Arch Linux state repository for fast package information
- Handles epoch versions (1:2.0-1), git revisions (1.0+r123.abc-1)
- Compares using packaging.version with fallback to string comparison
- Skips bootstrap-only packages (linux-api-headers, glibc, binutils, gcc) unless forced

**PKGBUILD Parsing**:
- Uses bash sourcing method to extract dependencies with variable expansion
- Extracts: depends, makedepends, checkdepends, provides arrays
- Filters dependencies to only include packages in build list
- Handles provides mapping for virtual dependencies

**Output Format**:
```json
{
  "_command": "command line used",
  "_timestamp": "ISO timestamp",
  "packages": [
    {
      "name": "package-name",
      "x86_version": "1.0-2",
      "arm_version": "1.0-1", 
      "basename": "pkgbase-name",
      "version": "1.0-2",
      "repo": "core|extra",
      "depends": ["dep1", "dep2"],
      "makedepends": ["makedep1"],
      "checkdepends": ["checkdep1"],
      "provides": ["virtual-pkg"],
      "force_latest": false,
      "use_aur": false,
      "use_local": false,
      "build_stage": 0,
      "skip": 0
    }
  ]
}
```

#### `build_packages.py`
**Purpose**: Build packages in clean chroot environments

**Key Functions**:
- Uses `makechrootpkg` with isolated build environments
- Handles dependency installation in temporary chroot copies
- Builds with `--ignorearch` flag for cross-architecture
- Manages temporary chroot cleanup with signal handling
- Uploads packages to appropriate testing repositories
- Supports repackaging failed builds without rebuilding

**Command Line Options**:
- `--dry-run`: Show actions without executing
- `--json FILE`: JSON file with packages to build
- `--blacklist FILE`: Skip packages matching patterns
- `--no-upload`: Build without uploading to repository
- `--cache DIR`: Custom pacman cache directory
- `--no-cache`: Clear cache before each build
- `--continue`: Continue from last successful package
- `--preserve-chroot`: Keep chroot after successful builds
- `--cleanup-on-failure`: Delete temporary chroots even on build failure
- `--stop-on-failure`: Stop on first failure
- `--chroot DIR`: Custom chroot directory
- `--parallel-jobs N`: Number of packages to build in parallel within the same stage (default: 1)

**Build Process**:
1. Create temporary chroot copy using `rsync`
2. Update package database with `arch-nspawn`
3. Install all dependencies (depends + makedepends + checkdepends)
4. Check if PKGBUILD arch array contains target architecture
5. Build with `makechrootpkg -l temp-{pkg}-{timestamp}` (with or without --ignorearch)
6. Upload to `{repo}-testing` repository
7. Clear built packages from cache to prevent stale/corrupted packages
8. Clean up temporary chroot (unless preserving)
9. Handle interruption signals gracefully

**Smart --ignorearch Handling**:
- Sources PKGBUILD and checks arch array
- Only uses --ignorearch if target architecture not in arch array
- Handles multi-line arch arrays correctly
- Prevents unnecessary --ignorearch usage for native packages

**Chroot Management**:
- Default chroot: `/scratch/builder`
- Default cache: `/scratch/builder/pacman-cache`
- Temporary chroots: `temp-{package}-{timestamp}`
- Uses `sudo rsync` for chroot copying
- Automatic cleanup on SIGINT/SIGTERM
- Parallel build support with isolated temporary chroots per package

#### `bootstrap_toolchain.py`
**Purpose**: Build core toolchain packages in staged approach

**Staged Build Process**:
- Stage 1: linux-api-headers → glibc → binutils → gcc → gmp → mpfr → libmpc → libisl
- Stage 2: glibc → binutils → gcc → libtool → valgrind

**Special Requirements**:
- gcc and glibc must be manually checked out from special repositories
- Uses lock file (bootstrap.lock) to prevent concurrent runs
- Clears pacman cache between builds to force using new packages
- Force installs toolchain dependencies before each build

**Command Line Options**:
- `--chroot DIR`: Custom chroot path
- `--cache DIR`: Custom cache directory  
- `--dry-run`: Show actions without executing
- `--continue`: Continue from last successful package
- `--start-from PACKAGE`: Start from specific package in stage 1
- `--no-update`: Skip git updates

#### `repo_analyze.py`
**Purpose**: Analyze repository differences and inconsistencies

**Analysis Types**:
- Repository mismatches (packages in wrong repo)
- Version differences (target architecture newer than x86_64)
- Architecture-specific packages (target architecture only)
- Outdated/missing ARCH=any packages
- Packages in multiple repositories
- Binary package version tracking (-bin packages)

**Color Coding**:
- Green: -bin packages matching x86_64 versions
- Cyan: -bin packages newer than x86_64
- Red: -bin packages outdated or aarch64-only packages in core/extra repos

**Command Line Options**:
- `--blacklist FILE`: Skip blacklisted packages
- `--use-existing-db`: Use existing database files instead of downloading
- `--missing-pkgbase`: Print missing package base names (space delimited)
- `--outdated-any`: Show outdated any packages
- `--missing-any`: Show missing any packages  
- `--repo-issues`: Show repository inconsistencies and duplicates
- `--target-newer`: Show packages where target architecture is newer
- `--target-only`: Show target architecture only packages

#### `find_dependents.py`
**Purpose**: Find packages that depend on a given package

**Usage**: `./find_dependents.py PACKAGE_NAME`
**Output**: Space-separated list of dependent package basenames

### 2. Utility Module (`utils.py`)

**Core Functions**:

**Package Validation**:
- `validate_package_name(name)`: Validates against Arch Linux naming rules
- `safe_path_join(base, user_input)`: Prevents directory traversal attacks

**Version Comparison**:
- `compare_arch_versions(v1, v2)`: Handles epochs, git revisions, semantic versions
- `is_version_newer(current, target)`: Returns True if target is newer
- `split_epoch_version(version)`: Separates epoch from version
- `has_git_revision(version)`: Detects git revision markers (+r)
- `compare_git_versions(v1, v2)`: Compares git revision versions

**Database Operations**:
- `parse_database_file(db_file, include_any=False)`: Parse pacman database
- `load_database_packages(urls, arch_suffix, download=True)`: Download and parse multiple databases
- `load_x86_64_packages(download=True, repos=None)`: Load x86_64 packages
- `load_target_arch_packages(download=True, urls=None)`: Load target architecture packages

**PKGBUILD Processing**:
- `parse_pkgbuild_deps(pkgbuild_path)`: Extract dependencies using bash sourcing

**Blacklist Management**:
- `load_blacklist(file)`: Load patterns with wildcard support
- `filter_blacklisted_packages(packages, blacklist)`: Filter using fnmatch

**Build Utilities (BuildUtils class)**:
- `run_command(cmd, cwd=None, capture_output=False)`: Execute with dry-run support
- `setup_chroot(chroot_path, cache_path)`: Create/setup build environment
- `upload_packages(pkg_dir, target_repo)`: Upload to S3 repository
- `cleanup_old_logs(package_name, keep_count=3)`: Manage build logs

**Configuration**:
- Reads `config.ini` for build paths and settings
- Default build root: `/scratch/builder`
- Default cache: `/scratch/builder/pacman-cache`
- Default x86_64 mirror: `https://geo.mirror.pkgbuild.com`
- Upload bucket: `arch-linux-repos.drzee.net`

### 3. Test Suite (`test_all.py`)

**Test Categories**:
- Package validation and security
- Version comparison logic
- Dependency parsing and resolution
- Package filtering and blacklists
- Build order calculation
- Chroot management
- Package upload functionality
- Configuration handling
- Error recovery and resilience
- Command-line option parsing
- Script integration testing
- Output format validation

**Test Execution**:
- Works with or without pytest
- 53 total tests covering all functionality
- Integration tests verify script imports and help commands
- Supports both unit tests and integration tests

## Data Flow

### Package Discovery Flow
1. Download x86_64 and target architecture database files (.db)
2. Parse databases to extract package metadata
3. Filter out ARCH=any packages
4. Compare versions between architectures
5. Apply blacklist filtering
6. Identify missing dependencies
7. Sort by dependency order using topological sort
8. Output JSON build list

### Build Flow
1. Read JSON package list
2. Setup chroot environment
3. **Build provides mapping for dependency failure propagation**
4. For each package:
   - **Check if package should be skipped due to failed dependencies**
   - **Handle cycle stage transitions (Stage 1 → Stage 2)**
   - Create temporary chroot copy
   - Install dependencies
   - Parse PKGBUILD for runtime dependency extraction
   - Build with makechrootpkg
   - Upload to testing repository
   - **Clear built packages from cache to prevent stale/corrupted packages**
   - Clean up temporary chroot
5. **Track cycle stage 1 successes for proper continue functionality**
6. Handle failures and interruptions gracefully

### Repository Structure
- **Official packages**: `https://gitlab.archlinux.org/archlinux/packaging/packages/{name}.git`
- **AUR packages**: `https://aur.archlinux.org/{name}.git`
- **State repository**: `https://gitlab.archlinux.org/archlinux/packaging/state`
- **Upload target**: S3 bucket with `repo-upload` tool

## Configuration Requirements

### `config.ini`
```ini
[build]
build_root = /scratch/builder
upload_bucket = arch-linux-repos.drzee.net
x86_64_mirror = https://geo.mirror.pkgbuild.com
```

### `chroot-config/pacman.conf`
Pacman configuration for chroot environment

### `chroot-config/makepkg.conf`
Makepkg configuration with build settings (MAKEFLAGS, etc.)

### Required Tools
- `makechrootpkg` (from devtools package)
- `pkgctl` (from devtools package)
- `arch-nspawn` (from devtools package)
- `repo-upload` (custom tool for S3 uploads)
- `git`, `rsync`, `wget`
- `python3` with `packaging` library

### System Requirements
- Sudo access for chroot operations
- 50GB+ disk space for chroot and cache
- 8GB+ RAM recommended
- Stable network connection

## Key Algorithms

### Topological Sort for Build Order
1. Build dependency graph from package metadata
2. Handle provides relationships for virtual dependencies
3. **Use Tarjan's algorithm to detect strongly connected components (cycles)**
4. **Create two-stage builds for cycle packages: Stage 1 (initial) and Stage 2 (final)**
5. **Assign cycle_group and cycle_stage metadata to cycle packages**
6. Calculate build stages using recursive depth-first search
7. Assign sequential build_stage numbers

### Version Comparison Algorithm
1. Split epoch from version string
2. Compare epochs numerically if different
3. Detect git revision patterns (+r123)
4. For git versions: compare base version, then revision number
5. Use packaging.version for standard semantic versions
6. Fallback to string comparison for malformed versions

### Dependency Resolution
1. Parse PKGBUILD using bash sourcing for variable expansion
2. Extract depends, makedepends, checkdepends arrays from both global and split package functions
3. Preserve provides information from upstream databases and merge with PKGBUILD provides
4. Filter to only include packages in current build list
5. Build provides mapping for virtual dependencies
6. Clean up version constraints from dependency names
7. **Use Tarjan's algorithm to detect strongly connected components (cycles)**
8. **Create two-stage builds for cycle packages (Stage 1 and Stage 2)**
9. **Implement dependency failure propagation to skip dependent packages when dependencies fail**

### Chroot Management
1. Create base chroot with mkarchroot if needed
2. For each build: rsync base to temporary copy
3. Install package-specific dependencies in temporary chroot
4. Build using makechrootpkg with temporary chroot
5. Clean up temporary chroot after build
6. Handle interruption signals for proper cleanup

## Error Handling Patterns

### Git Operations
- Retry with different tag formats for version checkout
- Fallback to latest commit if version tag not found
- Handle corrupted repositories by re-cloning
- Use git pull for updates (handles any default branch)

### Build Failures
- Preserve chroot for debugging on failure
- Save detailed build logs with timestamps
- Support repackaging without full rebuild
- Continue from last successful package
- **Dependency failure propagation: Skip packages when dependencies fail**
- **Automatic cache clearing after successful builds to prevent stale packages**

### Network Issues
- Retry downloads with wget
- Handle missing database files gracefully
- Validate downloaded files before parsing

### Resource Management
- Automatic cleanup of temporary files
- Log rotation (keep 3 most recent per package)
- Cache management with optional clearing
- Signal handling for graceful shutdown

## Security Considerations

### Package Name Validation
- Regex: `^[a-zA-Z0-9][a-zA-Z0-9+._-]*$`
- Prevents injection attacks and filesystem issues

### Path Safety
- Validate all user inputs for directory traversal
- Use safe_path_join for all path operations
- Resolve paths and check they stay within base directory

### Chroot Isolation
- Each package builds in isolated temporary chroot
- No persistence between package builds
- Clean dependency installation per package

### Privilege Management
- Sudo only for specific required operations
- No root execution of main scripts
- Minimal privilege escalation scope

This specification provides complete implementation details to recreate the system with identical functionality, behavior, and security characteristics.
