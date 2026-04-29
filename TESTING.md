# Test Suite Documentation

## Overview

The test suite (`test_all.py`) validates all components of the build system with **88 test cases** across **33 test classes**. It works with or without pytest installed.

## Running Tests

```bash
# Quick run (works without pytest)
python3 test_all.py

# With pytest (more detailed output)
python3 -m pytest test_all.py -v

# Specific category
python3 -m pytest test_all.py -k "Security" -v
python3 -m pytest test_all.py -k "Version" -v
python3 -m pytest test_all.py -k "Blacklist" -v
python3 -m pytest test_all.py -k "Dependency" -v
```

## Test Categories

### Security (TestSecurity — 4 tests)
- Valid package name acceptance
- Malicious package name rejection (path traversal, injection)
- Path traversal attack blocking via `safe_path_join()`
- Safe path allowance

### Version Comparison (TestVersionComparison — 4 tests, TestAdvancedVersionHandling — 2 tests, TestVersionComparisonPkgrel — 3 tests)
- Basic version comparison (1.0.0 vs 2.0.0)
- Epoch handling (1:2.0.0 vs 2.0.0)
- Git revision versions (1.0.0+r123.gabcdef)
- Malformed version resilience
- pkgrel comparison (1.0.0-1 vs 1.0.0-2)
- Epoch overriding pkgrel

### Build List Generation (TestBuildListGeneration — 1 test)
- Script exists and shows help

### Package Building (TestPackageBuilding — 2 tests)
- Script exists and shows help
- PKGBUILD dependency parsing returns correct structure

### Bootstrap Toolchain (TestBootstrapToolchain — 2 tests)
- Script exists and shows help
- Essential toolchain packages (glibc, gcc, binutils) are defined in STAGE1/STAGE2

### Dependency Resolution (TestDependencyResolution — 2 tests)
- Simple dependency chain ordering (A→B→C)
- Circular dependency handling (two-stage builds, 4 output packages for 2-package cycle)

### Dependency Graph (TestDependencyGraph — 3 tests)
- Deep dependency chains (A→B→C→D)
- Diamond dependency patterns (A→B,C→D)
- Provides relationships (virtual package resolution)

### Multiple Disconnected Cycles (TestMultipleDisconnectedCycles — 2 tests)
- Two independent cycles produce 8 packages (4×2 stages)
- Cycle with external dependency builds external first

### Provides Version Constraints (TestProvidesVersionConstraints — 2 tests)
- Version extraction from provides strings
- Build order respects provides relationships

### Configuration (TestConfiguration — 2 tests)
- Config file parsing
- Default values are reasonable (absolute paths)

### Error Handling (TestErrorHandling — 3 tests)
- Invalid JSON handling
- Missing file handling (load_blacklist returns empty list)
- Command failure handling

### Integration (TestIntegration — 2 tests)
- All main scripts show help without errors
- JSON output format round-trips correctly

### Database Parsing (TestDatabaseParsing — 3 tests)
- Missing .db file returns empty dict
- x86_64 package loading function exists
- Target arch package loading function exists

### PKGBUILD Processing (TestPKGBUILDProcessing — 2 tests, TestPKGBUILDParsingReal — 2 tests)
- Dependency extraction returns correct keys
- Variable expansion doesn't crash parser
- Real PKGBUILD content parsing (depends, makedepends, checkdepends)
- Variable expansion in dependencies (`${_somever}`)

### CLI Interfaces (TestCommandLineInterface — 3 tests)
- generate_build_list.py supports --packages, --blacklist, --use-latest, --no-update
- build_packages.py supports --dry-run, --no-upload, --cache, --continue, --chroot
- bootstrap_toolchain.py supports --dry-run, --chroot, --cache

### File Operations (TestFileOperations — 3 tests)
- JSON serialization/deserialization round-trip
- File path validation (valid and invalid names)
- Temporary file creation and cleanup

### Network Operations (TestNetworkOperations — 2 tests)
- Download error handling (no crash with download=False)
- URL format validation

### Build System (TestBuildSystem — 3 tests)
- Chroot path validation
- Build stage assignment for independent packages
- Package upload repo-to-testing mapping

### Architecture Detection (TestArchitectureDetection — 2 tests)
- Target architecture detection from makepkg.conf
- ARCH=any package filtering

### Blacklist Patterns (TestBlacklistPatterns — 4 tests)
- Wildcard suffix matching (`*-debug`, `*-git`)
- Wildcard prefix matching (`lib32-*`, `python-*`)
- Comment and empty line filtering in blacklist files
- `filter_blacklisted_packages()` applies patterns correctly

### Missing Dependencies (TestFindMissingDependencies — 4 tests)
- Finds missing direct dependencies
- Ignores satisfied dependencies
- Finds missing makedepends
- Respects provides relationships

### BuildUtils Class (TestBuildUtilsClass — 4 tests)
- Dry run mode returns success without executing
- `format_dry_run()` produces readable output
- `cleanup_old_logs()` keeps only N most recent logs
- `clear_packages_from_cache()` removes specific packages

### Package Upload Logic (TestPackageUploadLogic — 2 tests)
- Core/extra packages map to testing repos
- Package file detection (.pkg.tar.zst, excluding .sig)

### Bootstrap Lock File (TestBootstrapLockFile — 2 tests)
- Lock file contains PID information
- Stale lock detection (dead PID)

### Edge Cases (TestEdgeCases — 5 tests, TestBuildSystemEdgeCases — 3 tests)
- Empty package lists
- Circular dependency edge cases (self-dependency, 3-way cycles)
- Version comparison edge cases (empty strings, epochs, git revisions)
- Package name edge cases (dots, plus signs, leading dash/dot rejection)
- Chroot, package file, and dependency resolution edge cases

### Signal Handling (TestSignalHandlingEdgeCases — 3 tests)
- Signal handler registration
- Cleanup operations (files and directories)
- Lock file lifecycle

### Script Tests (TestRepoAnalyze — 1 test, TestFindDependentsScript — 1 test)
- repo_analyze.py exists and shows help
- find_dependents.py exists and shows help

### Utilities (TestUtilities — 5 tests)
- Blacklist loading
- Package filtering with wildcards
- String manipulation
- Path utilities
- Configuration parsing

## Test Structure

The test file uses a dual-mode runner:
- **With pytest**: Runs via `python3 -m pytest` with short tracebacks
- **Without pytest**: Uses a built-in `pytest.raises` replacement and iterates test classes manually

The `run_all_tests()` function also runs integration checks:
1. Verifies all core modules import successfully
2. Verifies all main scripts accept `--help`

## Areas Requiring Manual Testing

- Actual package building (requires chroot environment)
- Network operations (database downloads from mirrors)
- S3/DynamoDB operations (requires AWS credentials)
- GPG key operations
- Repository uploads via `repo-upload`
- `auto_builder.py` full cycle (requires all infrastructure)

## Note on Duplicate Class

`test_all.py` contains two `class TestEdgeCases` definitions. Python's class scoping means the second definition overrides the first. The effective test count (88) reflects the second definition's 5 tests, not the first's larger set. The first definition's unique tests (version comparison edge cases, package name edge cases with length limits, JSON/filesystem/network/config edge cases, build stage assignment, memory/performance, unicode) are lost at runtime.
