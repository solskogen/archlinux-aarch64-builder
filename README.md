# Arch Linux Multi-Architecture Builder

An automated build system that maintains ports of Arch Linux for multiple architectures (AArch64, RISC-V, s390x, etc.) by comparing package versions between x86_64 and target architectures, then building outdated packages in correct dependency order.

## Quick Start

```bash
# 1. Generate list of packages that need building
./generate_build_list.py

# 2. Build the packages
./build_packages.py
```

That's it! The system will automatically detect your target architecture, compare versions, and build packages in the correct order.

## Prerequisites

Before using this system, you need:

### Required Tools
```bash
sudo pacman -S devtools git rsync python-packaging
```

### Configuration Files
1. **`config.ini`** - Main configuration (see [Configuration](#configuration))
2. **`chroot-config/pacman.conf`** - Pacman configuration for build chroot
3. **`chroot-config/makepkg.conf`** - Build settings (must contain `CARCH=your_target_arch`)
4. **`package-overrides.json`** - Optional: Custom git repositories for specific packages

### System Requirements
- **Disk Space**: 50GB+ free space for chroot and cache
- **Memory**: 8GB+ recommended
- **Network**: Stable connection for downloads and uploads
- **Permissions**: Passwordless sudo for chroot management

## Key Concepts

**Target Architecture**: The architecture you're building packages for (e.g., aarch64, riscv64)

**Upstream Architecture**: The reference architecture to compare against (typically x86_64)

**Chroot Environment**: Isolated build environment that ensures clean, reproducible builds

**Testing Repositories**: Where built packages are uploaded first (core-testing, extra-testing) before manual promotion to stable repositories

## How Build Stages Work

The build system uses topological sorting to order packages by their dependencies - packages with no dependencies **on other packages in the build list** go in stage 0, packages that only depend on stage 0 packages go in stage 1, and so on. When circular dependencies are detected using Tarjan's algorithm (e.g., package A depends on B, B depends on A), those packages are built twice: first in an early stage to satisfy initial dependencies, then again in a later stage to link against the complete versions. The stage number represents the "depth" in the dependency tree - a package can only be built after all its dependencies from earlier stages are complete. Packages within the same stage can be built in parallel since they don't depend on each other. The system automatically detects these relationships by parsing PKGBUILD files and building a complete dependency graph before assigning stage numbers.

## Basic Workflow

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Compare Package │───▶│ Generate Build   │───▶│ Build Packages  │
│ Versions        │    │ List (JSON)      │    │ in Chroot       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         │
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Manual Promotion│◀───│ Upload to Testing│◀───│ Built .pkg.tar. │
│ to Stable Repos │    │ Repositories     │    │ zst Files       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Configuration

Create `config.ini` with your settings:

```ini
[build]
build_root = /tmp/builder
upload_bucket = your-s3-bucket.example.com
target_base_url = https://your-repo.com/arch

[repositories]
# Optional: Override default repository URLs
target_core_url = https://example.com/core/os/aarch64/core.db
target_extra_url = https://example.com/extra/os/aarch64/extra.db
```

**Configuration Options:**
- `build_root`: Directory for build operations and chroot
- `upload_bucket`: S3 bucket name for uploading built packages
- `target_base_url`: Base URL for your target architecture repositories

## Package Overrides

For packages that need custom git repositories or branches, create `package-overrides.json`:

```json
{
  "gcc": {
    "url": "https://gitlab.archlinux.org/solskogen/gcc.git",
    "branch": "experimental"
  },
  "glibc": {
    "url": "https://gitlab.archlinux.org/solskogen/glibc.git", 
    "branch": "aarch64"
  }
}
```

This allows you to:
- Use custom forks of packages with architecture-specific patches
- Track development branches instead of release tags
- Override the default Arch Linux GitLab repositories

When overrides are specified, the system will clone from the custom URL and checkout the specified branch instead of using version tags.

## Common Usage Examples

### Find and Build Outdated Packages
```bash
# Find packages where x86_64 is newer than target architecture
./generate_build_list.py

# Build all packages in dependency order
./build_packages.py
```

### Build Specific Packages
```bash
# Force rebuild specific packages
./generate_build_list.py --packages vim firefox gcc

# Build them
./build_packages.py
```

### Include Testing Repositories
```bash
# Compare against testing repos too
./generate_build_list.py --target-testing --upstream-testing
```

### Build from AUR
```bash
# Build AUR packages for your architecture
./generate_build_list.py --aur yay paru
./build_packages.py
```

### Analyze Repository Differences
```bash
# Show all differences between x86_64 and target architecture
./repo_analyze.py

# Show only missing packages
./repo_analyze.py --missing-pkgbase

# Show outdated ARCH=any packages
./repo_analyze.py --outdated-any

# Show target-only packages (with color coding for -bin packages)
./repo_analyze.py --target-only
```

### Dry Run (Test Without Building)
```bash
# See what would be built without actually building
./generate_build_list.py --packages vim gcc
./build_packages.py --dry-run
```

## Where Built Packages End Up

1. **Local Files**: Built `.pkg.tar.zst` files are created in `./pkgbuilds/{package}/`
2. **Testing Upload**: Packages are uploaded to testing repositories:
   - Core packages → `core-testing`
   - Extra packages → `extra-testing`
3. **S3 Storage**: Files are stored in the S3 bucket configured in `config.ini`
4. **Manual Promotion**: You must manually move packages from testing to stable repositories

## Validation Steps

Before building, verify your setup:

```bash
# Check required tools
which makechrootpkg pkgctl git rsync

# Verify configuration
cat config.ini

# Test chroot creation (dry run)
./build_packages.py --dry-run
```

## Advanced Usage

### Command Line Options

#### generate_build_list.py

| Option | Description |
|--------|-------------|
| `--packages PKG [PKG ...]` | Force rebuild specific packages by name |
| `--preserve-order` | Preserve exact order specified in --packages (skip dependency sorting) |
| `--local` | Build packages from local PKGBUILDs only (use with --packages) |
| `--aur PKG [PKG ...]` | Get specified packages from AUR (implies --packages mode) |
| `--blacklist FILE` | File containing packages to skip (default: blacklist.txt) |
| `--missing-packages` | List packages missing from target architecture repository |
| `--use-latest` | Use latest git commit instead of version tag |
| `--rebuild-repo REPO` | Rebuild all packages from specific repository (core/extra) |
| `--no-update` | Skip git updates, use existing PKGBUILDs |
| `--target-testing` | Also include target testing repos for comparison |
| `--upstream-testing` | Also include upstream testing repos for comparison |
| `--force` | Force rebuild ARCH=any packages (use with --packages) |

#### build_packages.py

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without executing |
| `--json FILE` | JSON file with packages to build (default: packages_to_build.json) |
| `--blacklist FILE` | File containing packages to skip |
| `--no-upload` | Build packages but don't upload to repository |
| `--cache DIR` | Custom pacman cache directory |
| `--no-cache` | Clear cache before each package build |
| `--continue` | Continue from last successful package |
| `--preserve-chroot` | Preserve chroot even on successful builds |
| `--cleanup-on-failure` | Delete temporary chroots even on build failure |
| `--stop-on-failure` | Stop building on first package failure |
| `--chroot DIR` | Custom chroot directory path |
| `--parallel-jobs N` | Number of packages to build in parallel within the same stage (default: 1) |

#### repo_analyze.py

| Option | Description |
|--------|-------------|
| `--blacklist FILE` | Blacklist file (default: blacklist.txt) |
| `--use-existing-db` | Use existing database files instead of downloading |
| `--missing-pkgbase` | Print missing pkgbase names (space delimited) |
| `--outdated-any` | Show outdated any packages |
| `--missing-any` | Show missing any packages |
| `--repo-issues` | Show repository inconsistencies and duplicates |
| `--target-newer` | Show packages where target architecture is newer |
| `--target-only` | Show target architecture only packages |

### Advanced Examples

#### Repository Migration
```bash
# Rebuild all core packages
./generate_build_list.py --rebuild-repo core
./build_packages.py --no-cache
```

#### Dependency Chain Rebuild
```bash
# Rebuild toolchain packages in order
./generate_build_list.py --packages glibc gcc binutils
./build_packages.py --no-cache
```

#### Continue Interrupted Build
```bash
# Resume from last successful package
./build_packages.py --continue
```

#### Bootstrap Toolchain
```bash
# Bootstrap core system packages
./bootstrap_toolchain.py
```

## Build Process Details

The build system:

- Uses existing PKGBUILDs from `./pkgbuilds/` directory (fetched by `generate_build_list.py`)
- Builds packages with `makechrootpkg` using `--ignorearch` flag
- Built `.pkg.tar.zst` files are created in the package's `./pkgbuilds/{package}/` directory
- Removes old package versions before upload (keeps only newest by modification time)
- Uploads to correct testing repository:
  - Core packages → `core-testing`
  - Extra packages → `extra-testing`
- Built packages are uploaded to S3 bucket configured in `config.ini` (`upload_bucket`)
- Packages must be manually promoted from testing repositories to stable repositories
- Preserves PKGBUILDs in `./pkgbuilds/` after successful builds
- Saves failed packages to `failed_packages.json` for retry
- Bootstrap mode: Clears pacman cache after each successful upload to force using newly uploaded packages
- Automatic log rotation: Keeps only the 3 most recent build logs per package with timestamps
- Imports GPG keys from `keys/pgp/` directory if present
- Signal handling: Properly cleans up temporary chroot copies on interruption

## Features

- **Automated Version Comparison**: Compares x86_64 vs target architecture package versions
- **Dependency Resolution**: Builds packages in correct dependency order using topological sort
- **Circular Dependency Handling**: Uses Tarjan's algorithm for two-stage builds of circular dependencies
- **Missing Dependency Detection**: Automatically includes missing dependencies in build list
- **Architecture Detection**: Automatically detects target architecture from makepkg.conf
- **Smart --ignorearch Handling**: Only uses --ignorearch when necessary (checks arch array in PKGBUILD)
- **Clean Chroot Builds**: Isolated build environments for reproducible results
- **Parallel Building**: Build multiple packages in parallel within the same dependency stage
- **Multiple Package Sources**: Supports official repos, AUR, and local packages
- **Binary Package Tracking**: Tracks -bin packages and compares versions with upstream
- **Progress Tracking**: Clear messages showing current vs target versions
- **Error Recovery**: Graceful handling of build failures and interruptions
- **Testing Integration**: Automatic upload to testing repositories with manual promotion workflow
- **Repository Analysis**: Comprehensive tools for analyzing repository differences and issues

## Troubleshooting

### Common Issues

**"Command not found" errors**
```bash
# Install missing tools
sudo pacman -S devtools git rsync python-packaging
```

**"Permission denied" errors**
```bash
# Set up passwordless sudo for required commands
sudo visudo
# Add: %wheel ALL=(ALL) NOPASSWD: /usr/bin/rm, /usr/bin/rsync, /usr/bin/arch-nspawn, /usr/bin/makechrootpkg
```

**"Chroot not found" errors**
```bash
# Remove and recreate build environment
sudo rm -rf /tmp/builder
./build_packages.py --dry-run  # This will recreate it
```

**"No packages to build" message**
```bash
# Check if packages are actually outdated
./generate_build_list.py --packages your-package

# Include testing repositories
./generate_build_list.py --target-testing --upstream-testing
```

**Build failures**
```bash
# Check build logs
ls logs/

# Continue from last successful package
./build_packages.py --continue

# Preserve chroot for debugging
./build_packages.py --preserve-chroot --stop-on-failure
```

**Network/download issues**
```bash
# Use existing database files
./generate_build_list.py --no-update

# Check network connectivity
ping geo.mirror.pkgbuild.com
```

### Expected Output Examples

**Successful package generation:**
```
Loading packages...
Loaded 2847 x86_64 packages (2234 pkgbase)
Loaded 2756 aarch64 packages (2198 pkgbase)
Comparing versions...
Found 23 packages where x86_64 is newer
Processing PKGBUILDs for complete dependency information...
[1/23] Processing vim (updating 9.1.1730-1 -> 9.1.1734-1)...
Sorting by build order...
Writing results to packages_to_build.json...
Complete!
```

**Successful build:**
```
Building 23 packages...
Setting up bootstrap environment...
Using existing chroot...

[1/23] Building vim
========================================
Creating temporary chroot: temp-vim-20241118151234
Installing checkdepends: python-setuptools
Running: makechrootpkg -l temp-vim-20241118151234 -r /tmp/builder
Successfully uploaded 1 packages to extra-testing
✓ vim built successfully
```

## Architecture Support

The system automatically detects your target architecture from `chroot-config/makepkg.conf` and supports:

- **AArch64** (ARM 64-bit)
- **RISC-V** (riscv64)
- **s390x** (IBM System z)
- **Any architecture** supported by Arch Linux

## Requirements

### System Dependencies
```bash
sudo pacman -S python-packaging devtools git rsync
```

### Configuration Files Required
- `chroot-config/pacman.conf` - Pacman configuration for chroot
- `chroot-config/makepkg.conf` - Makepkg configuration with build settings
- S3 credentials configured for `repo-upload` tool

### Performance Tuning

Edit `chroot-config/makepkg.conf`:
```bash
# Adjust based on CPU cores:
MAKEFLAGS="-j$(nproc)"
```

## License

This project is licensed under the BSD Zero Clause License - see the [LICENSE](LICENSE) file for details.
