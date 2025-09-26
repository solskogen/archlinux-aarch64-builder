# Arch Linux AArch64 Builder

This automated build system maintains an AArch64 port of Arch Linux by continuously monitoring package versions between x86_64 and AArch64 repositories, automatically identifying outdated packages, and building them in correct dependency order using clean chroot environments. It handles the complete workflow from package discovery to compilation and deployment to testing repositories.

## Features

- Uses Arch Linux state repository for fast, comprehensive package information
- Compares x86_64 (core + extra) against AArch64 repositories from configured mirrors
- Filters out architecture-independent packages (`ARCH=any`)
- Extracts comprehensive package metadata including source repository
- Fetches PKGBUILDs for complete dependency information with intelligent update detection
- Shows clear progress messages indicating current vs target versions (e.g., "updating 1.2.3 -> 1.2.4")
- Only performs git operations when package updates are actually needed
- Sorts packages by dependency-based build order using topological sort
- Automated building and uploading to correct testing repositories
- Configurable pacman cache directory for build optimization
- Bootstrap mode for dependency chain builds
- Detects missing dependencies and includes them automatically (always enabled)
- Detects repository inconsistencies (packages in both core and extra)
- Handles corrupted state repository by re-cloning automatically
- Generate lists of packages missing from AArch64 repository
- Repository analysis and version comparison tools
- Support for local, AUR, and official package sources
- Efficient processing: reads existing PKGBUILDs first to determine current versions before git operations

## Components

### Core Scripts

- **`generate_build_list.py`** - Downloads and parses Arch Linux package databases, compares x86_64 vs AArch64 versions, generates dependency-ordered build list
- **`build_packages.py`** - Main build orchestrator that processes packages in dependency order using clean chroot builds
- **`repo_analyze.py`** - Repository analysis tool for comparing versions and detecting inconsistencies between architectures
- **`bootstrap_toolchain.py`** - Specialized toolchain bootstrap builder for core system packages
- **`build_utils.py`** - Shared utilities for build scripts (command execution, logging, uploads)
- **`utils.py`** - Utility functions for blacklist management, package filtering, and database parsing

### Key Features

- **Clean Chroot Builds**: Uses `makechrootpkg` with isolated build environments
- **Cross-Architecture**: Builds x86_64 packages on AArch64 using `--ignorearch`
- **Test Dependencies**: Automatically handles `checkdepends` using temporary chroot copies when `!check` is used
- **Dependency Management**: Builds packages in topological dependency order
- **S3 Integration**: Uploads built packages to hosted staging repository
- **Configurable Cache**: Custom pacman cache directory for build optimization
- **Bootstrap Mode**: Clears cache after uploads to force using newly uploaded packages
- **State Repository**: Uses Arch Linux state repository for fast package information
- **Repository Consistency**: Detects and reports packages that exist in both core and extra (error condition)
- **Auto-Recovery**: Handles corrupted state repository by re-cloning automatically
- **GPG Key Import**: Automatically imports GPG keys from `keys/pgp/` directory
- **Signal Handling**: Properly cleans up temporary chroot copies on interruption

## Usage

### Package List Generation

#### Find packages needing updates
```bash
./generate_build_list.py
```

#### Rebuild specific packages (uses state repository version)
```bash
./generate_build_list.py --packages vim firefox gcc
```

#### Rebuild specific packages using latest git version
```bash
./generate_build_list.py --packages vim firefox gcc --use-latest
```

#### Use latest git version for all packages
```bash
./generate_build_list.py --use-latest
```

#### Rebuild specific packages from AUR
```bash
./generate_build_list.py --packages yay paru --aur
```

#### Build packages from existing PKGBUILDs (skip git updates)
```bash
./generate_build_list.py --packages my-custom-package --no-update
```

#### Build local packages (from pkgbuilds/ directory only)
```bash
./generate_build_list.py --packages my-custom-package --local
```

#### Exclude blacklisted packages
```bash
./generate_build_list.py --blacklist blacklist.txt
```

#### List packages missing from AArch64 repository
```bash
./generate_build_list.py --missing-packages
```

#### Rebuild all packages from specific repository
```bash
./generate_build_list.py --rebuild-repo core
./generate_build_list.py --rebuild-repo extra
```

#### Custom AArch64 repository URLs
```bash
./generate_build_list.py --arm-urls https://example.com/core.db https://example.com/extra.db
```

#### Skip git updates (use existing PKGBUILDs)
```bash
./generate_build_list.py --no-update
```

## Progress Messages

The system provides clear, informative progress messages during PKGBUILD processing:

- `Processing vim (fetching 9.1.1734-1)...` - Cloning new repository for specific version
- `Processing vim (already up to date: 9.1.1734-1)...` - Package is current, no git operations needed
- `Processing vim (updating 9.1.1730-1 -> 9.1.1734-1)...` - Updating from current to target version
- `Processing vim (updating to latest commit)...` - Updating AUR package or using `--use-latest`
- `Processing vim (no update, current: 9.1.1730-1)...` - Using `--no-update` flag
- `Processing vim (fetching from AUR)...` - Cloning new AUR package

This messaging system ensures users know exactly what operations are being performed and whether git updates are actually happening.

### Package Building

#### Build all packages from generated list
```bash
./build_packages.py
```

#### Build with options
```bash
./build_packages.py --dry-run --json custom_packages.json --blacklist blacklist.txt
```

#### Build without uploading to repository
```bash
./build_packages.py --no-upload
```

#### Build with custom cache directory
```bash
./build_packages.py --cache /custom/cache/path
```

#### Build in no-cache mode (clears cache before each package build)
```bash
./build_packages.py --no-cache
```

#### Continue interrupted build from last successful package
```bash
./build_packages.py --continue
```

#### Build with custom chroot path
```bash
./build_packages.py --chroot /custom/chroot/path
```

### Repository Analysis

#### Analyze version differences between architectures
```bash
./repo_analyze.py
```

#### Use custom blacklist file
```bash
./repo_analyze.py --blacklist custom-blacklist.txt
```

### Toolchain Bootstrap

#### Bootstrap build toolchain packages (separate script)
```bash
# First, manually checkout special toolchain repositories:
git clone <special-gcc-repo-url> pkgbuilds/gcc
git clone <special-glibc-repo-url> pkgbuilds/glibc

./bootstrap_toolchain.py
```

#### Bootstrap with options
```bash
./bootstrap_toolchain.py --dry-run --chroot /custom/chroot --cache /custom/cache
```

#### Bootstrap starting from specific package
```bash
# Skip linux-api-headers if no update
./bootstrap_toolchain.py --start-from glibc

# Skip toolchain, only rebuild math libs + stage 2
./bootstrap_toolchain.py --start-from gmp
```

## Use Cases

### 1. Regular Package Updates
**Scenario**: Update all packages that have newer versions in x86_64 repositories.

```bash
# Generate list of packages needing updates
./generate_build_list.py

# Build all packages in dependency order
./build_packages.py
```

### 2. Specific Package Rebuild
**Scenario**: A specific package like `gcc` needs to be rebuilt due to a bug fix.

```bash
# Rebuild gcc using the version from state repository
./generate_build_list.py --packages gcc

# Build the package
./build_packages.py
```

### 3. Testing Latest Development Version
**Scenario**: Test the latest development version of `vim` from GitLab main branch.

```bash
# Get latest vim from GitLab main branch
./generate_build_list.py --packages vim --use-latest

# Build with no cache to ensure fresh dependencies
./build_packages.py --no-cache
```

### 4. AUR Package Integration
**Scenario**: Build AUR packages like `yay` for the AArch64 repository.

```bash
# Get yay from AUR
./generate_build_list.py --packages yay --aur

# Build the AUR package
./build_packages.py
```

### 5. Local Package Development
**Scenario**: Build and test a custom package from local PKGBUILD.

```bash
# Prepare PKGBUILD in pkgbuilds/my-package/PKGBUILD
mkdir -p pkgbuilds/my-package
# ... create PKGBUILD ...

# Build local package
./generate_build_list.py --packages my-package --local
./build_packages.py
```

### 6. Repository Migration
**Scenario**: Rebuild all core packages for a major update.

```bash
# Get all core packages
./generate_build_list.py --rebuild-repo core

# Build with fresh cache
./build_packages.py --no-cache
```

### 7. Dependency Chain Rebuild
**Scenario**: A core library like `glibc` was updated and dependent packages need rebuilding.

```bash
# Find packages that depend on glibc (manual identification)
./generate_build_list.py --packages glibc gcc binutils

# Build in dependency order with fresh cache
./build_packages.py --no-cache
```

### 8. Missing Package Discovery
**Scenario**: Find packages available in x86_64 but missing from AArch64.

```bash
# Generate list of missing packages
./generate_build_list.py --missing-packages > missing_packages.json

# Review and selectively build missing packages
./generate_build_list.py --packages package1 package2 package3
./build_packages.py
```

### 9. Repository Analysis
**Scenario**: Analyze differences between x86_64 and AArch64 repositories.

```bash
# Comprehensive repository analysis
./repo_analyze.py

# Shows:
# - Packages where AArch64 is newer than x86_64
# - Packages that exist only in AArch64
# - Repository mismatches (packages in wrong repo)
```

### 10. Toolchain Bootstrap
**Scenario**: Bootstrap a complete toolchain from scratch.

```bash
# First, manually checkout special toolchain repositories
git clone <special-gcc-repo> pkgbuilds/gcc
git clone <special-glibc-repo> pkgbuilds/glibc

# Bootstrap the toolchain
./bootstrap_toolchain.py
```

### 11. Interrupted Build Recovery
**Scenario**: A long build process was interrupted and needs to continue.

```bash
# Continue from where it left off
./build_packages.py --continue
```

### 12. Development Testing
**Scenario**: Test build process without actually building packages.

```bash
# Dry run to see what would be built
./generate_build_list.py --packages vim gcc
./build_packages.py --dry-run
```

## Command Line Options

### generate_build_list.py

| Option | Description |
|--------|-------------|
| `--arm-urls URL [URL ...]` | URLs for AArch64 repository databases |
| `--packages PKG [PKG ...]` | Force rebuild specific packages by name |
| `--local` | Build packages from local PKGBUILDs only (use with --packages) |
| `--aur` | Use AUR as source for packages specified with --packages |
| `--blacklist FILE` | File containing packages to skip (default: blacklist.txt) |
| `--missing-packages` | List packages missing from AArch64 repository |
| `--use-latest` | Use latest git commit instead of version tag |
| `--rebuild-repo REPO` | Rebuild all packages from specific repository (core/extra) |
| `--no-update` | Skip git updates, use existing PKGBUILDs |

### build_packages.py

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without executing |
| `--json FILE` | JSON file with packages to build (default: packages_to_build.json) |
| `--blacklist FILE` | File containing packages to skip |
| `--no-upload` | Build packages but don't upload to repository |
| `--cache DIR` | Custom pacman cache directory |
| `--no-cache` | Clear cache before each package build |
| `--continue` | Continue from last successful package |
| `--stop-on-failure` | Stop building on first package failure |
| `--chroot DIR` | Custom chroot directory path |

### repo_analyze.py

| Option | Description |
|--------|-------------|
| `--blacklist FILE` | File containing packages to skip (default: blacklist.txt) |

### bootstrap_toolchain.py

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without executing |
| `--chroot DIR` | Custom chroot directory path |
| `--cache DIR` | Custom pacman cache directory |

## Build Process

The `build_packages.py` script:

- Uses existing chroot environment or creates new one if needed
- Configures custom pacman cache directory (default: `/var/tmp/builder/pacman-cache`)
- For packages with checkdepends (when `!check` is used in PKGBUILD):
  - Creates temporary chroot copy using `sudo rsync`
  - Installs checkdepends with `sudo arch-nspawn` and `pacman -Syy`
  - Uses `makechrootpkg -l` with temporary copy
  - Cleans up temporary copy automatically (even on Ctrl+C)
- Updates chroot automatically via `makechrootpkg -u` before each build
- Uses existing PKGBUILDs from `./pkgbuilds/` directory (fetched by `generate_build_list.py`)
- Builds packages with `makechrootpkg` using `--ignorearch` flag
- Removes old package versions before upload (keeps only newest by modification time)
- Uploads to correct testing repository:
  - Core packages → `core-testing`
  - Extra packages → `extra-testing`
- Preserves PKGBUILDs in `./pkgbuilds/` after successful builds
- Saves failed packages to `failed_packages.json` for retry
- Bootstrap mode: Clears pacman cache after each successful upload to force using newly uploaded packages
- Automatic log rotation: Keeps only the 3 most recent build logs per package with timestamps
- Imports GPG keys from `keys/pgp/` directory if present
- Signal handling: Properly cleans up temporary chroot copies on interruption

## Repository Analysis

The `repo_analyze.py` script provides comprehensive analysis:

1. **Repository Mismatches**: Identifies packages that are in the wrong repository (e.g., core packages in extra)
2. **Version Differences**: Shows packages where AArch64 has newer versions than x86_64
3. **Architecture-Specific Packages**: Lists packages that exist only in AArch64 (after filtering out provides relationships)

## Build Order

Packages are sorted using topological sort to ensure:
- Dependencies are built before dependent packages
- Packages providing virtual dependencies come first
- Circular dependencies are handled gracefully
- Safe sequential building without conflicts

## No-Cache Mode

When using `--no-cache`, the pacman cache is cleared before building each package. This ensures:
- Each package build uses newly uploaded packages from the repository
- No stale cached packages interfere with dependency resolution
- Essential for building dependency chains where later packages need updated versions

## Toolchain Bootstrap

The `bootstrap_toolchain.py` script builds core toolchain packages in a staged approach to ensure all components are compiled with each other:

**Stage 1 - Initial Build**: linux-api-headers → glibc → binutils → gcc → gmp → mpfr → libmpc → libisl

**Stage 2 - Final Rebuild**: glibc → binutils → gcc → libtool → valgrind

**Special Requirements**:
- `gcc` and `glibc` must be manually checked out from their special repositories
- Other packages are automatically cloned from Arch Linux GitLab if missing
- Uses lock file (bootstrap.lock) to prevent multiple simultaneous runs
- Shows progress indication per stage and package
- Graceful cleanup on interruption (SIGINT/SIGTERM)
- Updates chroot package database before each build
- Force installs latest toolchain dependencies
- Clears pacman cache before starting and between builds
- Stops immediately on any command failure with detailed error messages
- Uploads packages to correct testing repositories (core-testing or extra-testing)

## Output

The program outputs JSON with packages where x86_64 has newer versions, sorted by build order:

### Sample JSON Output

```json
{
  "_command": "./generate_build_list.py --packages vim",
  "_timestamp": "2025-09-18T10:13:57.735Z",
  "packages": [
    {
      "name": "boost-libs",
      "x86_version": "1.86.0-2",
      "arm_version": "1.86.0-1",
      "basename": "boost",
      "version": "1.86.0-2",
      "repo": "extra",
      "depends": [
        "bzip2",
        "zlib",
        "zstd"
      ],
      "makedepends": [
        "icu",
        "python",
        "python-numpy",
        "bzip2",
        "zlib",
        "openmpi",
        "zstd"
      ],
      "provides": [
        "libboost_atomic.so=1.86.0-64",
        "libboost_chrono.so=1.86.0-64",
        "libboost_container.so=1.86.0-64"
      ],
      "force_latest": false,
      "use_aur": false,
      "use_local": false,      "build_stage": 0
    }
  ]
}
```

## JSON Fields

- `name`: Package name
- `x86_version`: Version in x86_64 repository
- `arm_version`: Version in AArch64 repository  
- `basename`: Base package name (important for split packages)
- `version`: Package version string
- `repo`: Source repository (core or extra)
- `depends`: Runtime dependencies
- `makedepends`: Build-time dependencies
- `provides`: Virtual packages/libraries provided
- `force_latest`: Use latest git commit instead of version tag
- `use_aur`: Clone from AUR instead of Arch Linux GitLab
- `use_local`: Use local PKGBUILD from pkgbuilds/ directory- `build_stage`: Build order stage number
- `skip`: If set to 1, package is blacklisted and should be skipped during build

## JSON Format

The output JSON contains metadata and an array of packages to build:

```json
{
  "_command": "./generate_build_list.py --packages vim",
  "_timestamp": "2025-09-19T10:13:57.735Z",
  "packages": [
    {
      "name": "boost-libs",
      "x86_version": "1.86.0-2",
      "arm_version": "1.86.0-1",
      "basename": "boost",
      "version": "1.86.0-2",
      "repo": "extra",
      "depends": ["bzip2", "zlib", "zstd"],
      "makedepends": ["icu", "python", "python-numpy"],
      "provides": ["libboost_atomic.so=1.86.0-64"],
      "force_latest": false,
      "use_aur": false,
      "build_stage": 0,
      "skip": 0
    }
  ]
}
```

**Blacklisted packages** are included in the JSON with `"skip": 1` for visibility but are automatically skipped during builds.

## Troubleshooting

### Common Issues

**Lock File Errors**
```bash
# If bootstrap.lock exists but no process is running:
rm bootstrap.lock

# Check if bootstrap process is actually running:
ps aux | grep bootstrap_toolchain
```

**Sudo Permission Issues**
```bash
# Ensure passwordless sudo for required commands:
sudo visudo
# Add: %wheel ALL=(ALL) NOPASSWD: /usr/bin/rm, /usr/bin/rsync, /usr/bin/arch-nspawn, /usr/bin/makechrootpkg
```

**Missing Tools**
```bash
# Install all required tools:
sudo pacman -S devtools git rsync python-packaging

# Verify tools are available:
which makechrootpkg pkgctl git rsync
```

**Chroot Issues**
```bash
# Remove entire build environment:
sudo rm -rf /var/tmp/builder

# Or just clear cache:
sudo rm -rf /var/tmp/builder/pacman-cache/*
```

**GPG Key Issues**
```bash
# Import missing GPG keys manually:
gpg --recv-keys <key-id>

# Or place .asc files in pkgbuilds/<package>/keys/pgp/
```

### Resource Requirements

- **Disk Space**: Minimum 50GB free space for chroot and cache
- **Memory**: 8GB+ recommended for parallel builds
- **Build Time**: 1-4 hours per package depending on complexity
- **Network**: Stable connection for downloading sources and uploading packages

### Performance Tuning

Edit `chroot-config/makepkg.conf`:
```bash
# Adjust based on CPU cores:
MAKEFLAGS="-j$(nproc)"
```

## Requirements

- Python 3.x
- `python-packaging` library for version comparison
- `devtools` package for `makechrootpkg` and `pkgctl`
- `git` for repository operations
- `rsync` for chroot operations
- `sudo` access for chroot management
- `repo-upload` tool for uploading to repositories
- S3 bucket configuration for package uploads

```bash
sudo pacman -S python-packaging devtools git rsync
```

### Configuration Files Required

- `chroot-config/pacman.conf` - Pacman configuration for chroot
- `chroot-config/makepkg.conf` - Makepkg configuration with build settings
- S3 credentials configured for `repo-upload` tool

## License

This project is licensed under the BSD Zero Clause License - see the [LICENSE](LICENSE) file for details.
./generate_build_list.py
```

### Rebuild specific packages (uses state repository version)
```bash
./generate_build_list.py --packages vim firefox gcc
```

### Rebuild specific packages using latest git version
```bash
./generate_build_list.py --packages vim firefox gcc --use-latest
```

### Use latest git version for all packages
```bash
./generate_build_list.py --use-latest
```

### Rebuild specific packages from AUR
```bash
./generate_build_list.py --packages yay paru --aur
```

### Exclude blacklisted packages
```bash
./generate_build_list.py --blacklist blacklist.txt
```

### List packages missing from AArch64 repository
```bash
./generate_build_list.py --missing-packages
```

### Rebuild all packages from specific repository
```bash
./generate_build_list.py --rebuild-repo core
./generate_build_list.py --rebuild-repo extra
```

### Build all packages from generated list
```bash
./build_packages.py
```

### Build with options
```bash
./build_packages.py --dry-run --json custom_packages.json --blacklist blacklist.txt
```

### Build without uploading to repository
```bash
./build_packages.py --no-upload
```

### Build with custom cache directory
```bash
./build_packages.py --cache /custom/cache/path
```

### Build in no-cache mode (clears cache before each package build)
```bash
./build_packages.py --no-cache
```

### Continue interrupted build from last successful package
```bash
./build_packages.py --continue
```

### Bootstrap build toolchain packages (separate script)
```bash
# First, manually checkout special toolchain repositories:
git clone <special-gcc-repo-url> pkgbuilds/gcc
git clone <special-glibc-repo-url> pkgbuilds/glibc

./bootstrap_toolchain.py
```

### Bootstrap with options
```bash
./bootstrap_toolchain.py --dry-run --chroot /custom/chroot --cache /custom/cache
```

## Use Cases

### 1. Regular Package Updates
**Scenario**: Update all packages that have newer versions in x86_64 repositories.

```bash
# Generate list of packages needing updates
./generate_build_list.py

# Build all packages in dependency order
./build_packages.py
```

### 2. Specific Package Rebuild
**Scenario**: A specific package like `gcc` needs to be rebuilt due to a bug fix.

```bash
# Rebuild gcc using the version from state repository
./generate_build_list.py --packages gcc

# Build the package
./build_packages.py
```

### 3. Testing Latest Development Version
**Scenario**: Test the latest development version of `vim` from GitLab main branch.

```bash
# Get latest vim from GitLab main branch
./generate_build_list.py --packages vim --use-latest

# Build with no cache to ensure fresh dependencies
./build_packages.py --no-cache
```

### 4. AUR Package Integration
**Scenario**: Build AUR packages like `yay` for the AArch64 repository.

```bash
# Get yay from AUR
./generate_build_list.py --packages yay --aur

# Build the AUR package
./build_packages.py
```

### 5. Repository Migration
**Scenario**: Rebuild all core packages for a major update.

```bash
# Get all core packages
./generate_build_list.py --rebuild-repo core

# Build with fresh cache
./build_packages.py --no-cache
```

### 6. Dependency Chain Rebuild
**Scenario**: A core library like `glibc` was updated and dependent packages need rebuilding.

```bash
# Find packages that depend on glibc (manual identification)
./generate_build_list.py --packages glibc gcc binutils

# Build in dependency order with fresh cache
./build_packages.py --no-cache
```

### 7. Missing Package Discovery
**Scenario**: Find packages available in x86_64 but missing from AArch64.

```bash
# Generate list of missing packages
./generate_build_list.py --missing-packages > missing_packages.json

# Review and selectively build missing packages
./generate_build_list.py --packages package1 package2 package3
./build_packages.py
```

### 8. Toolchain Bootstrap
**Scenario**: Bootstrap a complete toolchain from scratch.

```bash
# First, manually checkout special toolchain repositories
git clone <special-gcc-repo> pkgbuilds/gcc
git clone <special-glibc-repo> pkgbuilds/glibc

# Bootstrap the toolchain
./bootstrap_toolchain.py
```

### 9. Interrupted Build Recovery
**Scenario**: A long build process was interrupted and needs to continue.

```bash
# Continue from where it left off
./build_packages.py --continue
```

### 10. Development Testing
**Scenario**: Test build process without actually building packages.

```bash
# Dry run to see what would be built
./generate_build_list.py --packages vim gcc
./build_packages.py --dry-run
```

## Build Process

The `build_packages.py` script:

- Uses existing chroot environment or creates new one if needed
- Configures custom pacman cache directory (default: `/var/tmp/builder/pacman-cache`)
- For packages with checkdepends (when `!check` is used in PKGBUILD):
  - Creates temporary chroot copy using `sudo rsync`
  - Installs checkdepends with `sudo arch-nspawn` and `pacman -Syy`
  - Uses `makechrootpkg -l` with temporary copy
  - Cleans up temporary copy automatically (even on Ctrl+C)
- Updates chroot automatically via `makechrootpkg -u` before each build
- Uses existing PKGBUILDs from `./pkgbuilds/` directory (fetched by `generate_build_list.py`)
- Builds packages with `makechrootpkg` using `--ignorearch` flag
- Removes old package versions before upload (keeps only newest by modification time)
- Uploads to correct testing repository:
  - Core packages → `core-testing`
  - Extra packages → `extra-testing`
- Preserves PKGBUILDs in `./pkgbuilds/` after successful builds
- Saves failed packages to `failed_packages.json` for retry
- Bootstrap mode: Clears pacman cache after each successful upload to force using newly uploaded packages
- Automatic log rotation: Keeps only the 3 most recent build logs per package with timestamps
- Imports GPG keys from `keys/pgp/` directory if present
- Signal handling: Properly cleans up temporary chroot copies on interruption

## Build Order

Packages are sorted using topological sort to ensure:
- Dependencies are built before dependent packages
- Packages providing virtual dependencies come first
- Circular dependencies are handled gracefully
- Safe sequential building without conflicts

## No-Cache Mode

When using `--no-cache`, the pacman cache is cleared before building each package. This ensures:
- Each package build uses newly uploaded packages from the repository
- No stale cached packages interfere with dependency resolution
- Essential for building dependency chains where later packages need updated versions

## Toolchain Bootstrap

The `bootstrap_toolchain.py` script builds core toolchain packages in a staged approach to ensure all components are compiled with each other:

**Stage 1 - Initial Build**: linux-api-headers → glibc → binutils → gcc → gmp → mpfr → libmpc → libisl

**Stage 2 - Final Rebuild**: glibc → binutils → gcc → libtool → valgrind

**Special Requirements**:
- `gcc` and `glibc` must be manually checked out from their special repositories
- Other packages are automatically cloned from Arch Linux GitLab if missing
- Uses lock file (bootstrap.lock) to prevent multiple simultaneous runs
- Shows progress indication per stage and package
- Graceful cleanup on interruption (SIGINT/SIGTERM)
- Updates chroot package database before each build
- Force installs latest toolchain dependencies
- Clears pacman cache before starting and between builds
- Stops immediately on any command failure with detailed error messages
- Uploads packages to correct testing repositories (core-testing or extra-testing)

**Usage**:
```bash
# First, manually checkout special repos:
# git clone <special-gcc-repo> pkgbuilds/gcc
# git clone <special-glibc-repo> pkgbuilds/glibc

./bootstrap_toolchain.py
```

## Output

The program outputs JSON with packages where x86_64 has newer versions, sorted by build order:

### Sample JSON Output

```json
{
  "_command": "./generate_build_list.py --packages vim",
  "_timestamp": "2025-09-18T10:13:57.735Z",
  "packages": [
    {
      "name": "boost-libs",
      "x86_version": "1.86.0-2",
      "arm_version": "1.86.0-1",
      "basename": "boost",
      "version": "1.86.0-2",
      "repo": "extra",
      "depends": [
        "bzip2",
        "zlib",
        "zstd"
      ],
      "makedepends": [
        "icu",
        "python",
        "python-numpy",
        "bzip2",
        "zlib",
        "openmpi",
        "zstd"
      ],
      "provides": [
        "libboost_atomic.so=1.86.0-64",
        "libboost_chrono.so=1.86.0-64",
        "libboost_container.so=1.86.0-64"
      ],
      "force_latest": false,
      "use_aur": false,
      "build_stage": 0
    }
      "use_local": false,  ]
}
```

## JSON Fields

- `name`: Package name
- `x86_version`: Version in x86_64 repository
- `arm_version`: Version in AArch64 repository  
- `basename`: Base package name (important for split packages)
- `version`: Package version string
- `repo`: Source repository (core or extra)
- `depends`: Runtime dependencies
- `makedepends`: Build-time dependencies
- `provides`: Virtual packages/libraries provided
- `force_latest`: Use latest git commit instead of version tag
- `use_aur`: Clone from AUR instead of Arch Linux GitLab
- `build_stage`: Build order stage number
- `skip`: If set to 1, package is blacklisted and should be skipped during build
- `use_local`: Use local PKGBUILD from pkgbuilds/ directory
## JSON Format

The output JSON contains metadata and an array of packages to build:

```json
{
  "_command": "./generate_build_list.py --packages vim",
  "_timestamp": "2025-09-19T10:13:57.735Z",
  "packages": [
    {
      "name": "boost-libs",
      "x86_version": "1.86.0-2",
      "arm_version": "1.86.0-1",
      "basename": "boost",
      "version": "1.86.0-2",
      "repo": "extra",
      "depends": ["bzip2", "zlib", "zstd"],
      "makedepends": ["icu", "python", "python-numpy"],
      "provides": ["libboost_atomic.so=1.86.0-64"],
      "force_latest": false,
      "use_aur": false,
      "build_stage": 0,
      "skip": 0
    }
  ]
}
```

**Blacklisted packages** are included in the JSON with `"skip": 1` for visibility but are automatically skipped during builds.

## Troubleshooting

### Common Issues

**Lock File Errors**
```bash
# If bootstrap.lock exists but no process is running:
rm bootstrap.lock

# Check if bootstrap process is actually running:
ps aux | grep bootstrap_toolchain
```

**Sudo Permission Issues**
```bash
# Ensure passwordless sudo for required commands:
sudo visudo
# Add: %wheel ALL=(ALL) NOPASSWD: /usr/bin/rm, /usr/bin/rsync, /usr/bin/arch-nspawn, /usr/bin/makechrootpkg
```

**Missing Tools**
```bash
# Install all required tools:
sudo pacman -S devtools git rsync python-packaging

# Verify tools are available:
which makechrootpkg pkgctl git rsync
```

**Chroot Issues**
```bash
# Remove entire build environment:
sudo rm -rf /var/tmp/builder

# Or just clear cache:
sudo rm -rf /var/tmp/builder/pacman-cache/*
```

**GPG Key Issues**
```bash
# Import missing GPG keys manually:
gpg --recv-keys <key-id>

# Or place .asc files in pkgbuilds/<package>/keys/pgp/
```

### Resource Requirements

- **Disk Space**: Minimum 50GB free space for chroot and cache
- **Memory**: 8GB+ recommended for parallel builds
- **Build Time**: 1-4 hours per package depending on complexity
- **Network**: Stable connection for downloading sources and uploading packages

### Performance Tuning

Edit `chroot-config/makepkg.conf`:
```bash
# Adjust based on CPU cores:
MAKEFLAGS="-j$(nproc)"
```

## Requirements

- Python 3.x
- `python-packaging` library for version comparison
- `devtools` package for `makechrootpkg` and `pkgctl`
- `git` for repository operations
- `rsync` for chroot operations
- `sudo` access for chroot management
- `repo-upload` tool for uploading to repositories
- S3 bucket configuration for package uploads

```bash
sudo pacman -S python-packaging devtools git rsync
```

### Configuration Files Required

- `chroot-config/pacman.conf` - Pacman configuration for chroot
- `chroot-config/makepkg.conf` - Makepkg configuration with build settings
- S3 credentials configured for `repo-upload` tool
