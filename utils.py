#!/usr/bin/env python3
"""
Shared utility functions for the Arch Linux AArch64 build system.

This module provides common functionality used across multiple scripts:
- Package name validation and security
- Version comparison with Arch Linux semantics  
- Database loading and parsing
- Blacklist management
- Path safety utilities
- Build utilities (command execution, chroot management, package uploads)

The utilities handle various edge cases in Arch Linux package management
including epoch versions, git revisions, and architecture filtering.
"""

import os
import fnmatch
import subprocess
import sys
import re
import configparser
import threading
import tarfile
import concurrent.futures
from pathlib import Path
from packaging import version

# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

# Configuration constants
BUILD_ROOT = config.get('build', 'build_root', fallback='/scratch/builder')
CACHE_PATH = f"{BUILD_ROOT}/pacman-cache"
UPLOAD_BUCKET = config.get('build', 'upload_bucket')
X86_64_MIRROR = config.get('build', 'x86_64_mirror', fallback='https://geo.mirror.pkgbuild.com')
LOG_RETENTION_COUNT = 3

# Directory constants
PKGBUILDS_DIR = "pkgbuilds"
LOGS_DIR = "logs"

# Build constants
TEMP_CHROOT_ID_MIN = 1000000  # 7-digit random ID range for temp chroots
TEMP_CHROOT_ID_MAX = 9999999
SEPARATOR_WIDTH = 60          # Width of === separator lines
GIT_COMMAND_TIMEOUT = 10      # Seconds to wait for git commands

# Constants
PACKAGE_SKIP_FLAG = 1

def get_target_architecture():
    """Read target architecture from chroot-config/makepkg.conf"""
    makepkg_conf = Path("chroot-config/makepkg.conf")
    if not makepkg_conf.exists():
        print("ERROR: chroot-config/makepkg.conf not found")
        sys.exit(1)
    
    try:
        with open(makepkg_conf, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('CARCH='):
                    # Extract value from CARCH="value"
                    return line.split('=')[1].strip('"\'')
        
        print("ERROR: CARCH not found in chroot-config/makepkg.conf")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to read chroot-config/makepkg.conf: {e}")
        sys.exit(1)

def validate_package_name(pkg_name: str) -> bool:
    """
    Validate package name against Arch Linux naming rules.
    
    Ensures package names only contain safe characters to prevent
    injection attacks and filesystem issues.
    
    Args:
        pkg_name: Package name to validate
        
    Returns:
        bool: True if name is valid, False otherwise
    """
    VALID_PKG_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9+._-]*$')
    return bool(VALID_PKG_NAME.match(pkg_name))

def safe_path_join(base: Path, user_input: str) -> Path:
    """
    Safely join paths preventing traversal attacks.
    
    Validates the user input and ensures the resulting path
    stays within the base directory to prevent directory
    traversal security vulnerabilities.
    
    Args:
        base: Base directory path
        user_input: User-provided path component
        
    Returns:
        Path: Safe joined path
        
    Raises:
        ValueError: If path traversal is detected or name is invalid
    """
    if not validate_package_name(user_input):
        raise ValueError(f"Invalid package name: {user_input}")
    
    path = base / user_input
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Path traversal detected")
    return path

class ArchVersionComparator:
    """Centralized version comparison for Arch Linux packages"""
    
    @staticmethod
    def compare(version1: str, version2: str) -> int:
        """
        Compare two Arch Linux version strings.
        Returns: -1 if version1 < version2, 0 if equal, 1 if version1 > version2
        """
        epoch1, ver1 = ArchVersionComparator._split_epoch_version(version1)
        epoch2, ver2 = ArchVersionComparator._split_epoch_version(version2)
        
        # Compare epochs first
        if epoch1 != epoch2:
            return -1 if epoch1 < epoch2 else 1
        
        # Handle git revision versions
        if ArchVersionComparator._has_git_revision(ver1) or ArchVersionComparator._has_git_revision(ver2):
            return ArchVersionComparator._compare_git_versions(ver1, ver2)
        
        # Use packaging.version for standard versions
        try:
            v1 = version.parse(ver1)
            v2 = version.parse(ver2)
            if v1 < v2:
                return -1
            elif v1 > v2:
                return 1
            return 0
        except Exception:
            # Fallback to string comparison
            return -1 if ver1 < ver2 else (1 if ver1 > ver2 else 0)
    
    @staticmethod
    def is_newer(current_version: str, target_version: str) -> bool:
        """Return True if target_version is newer than current_version"""
        return ArchVersionComparator.compare(current_version, target_version) < 0
    
    @staticmethod
    def _split_epoch_version(version_str: str) -> tuple:
        """Split version string into epoch and version parts"""
        if ':' in version_str:
            epoch_str, ver_str = version_str.split(':', 1)
            return int(epoch_str), ver_str
        return 0, version_str
    
    @staticmethod
    def _has_git_revision(version_str: str) -> bool:
        """Check if version contains git revision marker"""
        return '+r' in version_str
    
    @staticmethod
    def _compare_git_versions(ver1: str, ver2: str) -> int:
        """Compare versions that may contain git revisions"""
        # Extract base versions
        base1 = re.split(r'\+r\d+', ver1)[0]
        base2 = re.split(r'\+r\d+', ver2)[0]
        
        try:
            base_v1 = version.parse(base1)
            base_v2 = version.parse(base2)
            
            if base_v1 != base_v2:
                return -1 if base_v1 < base_v2 else 1
            
            # Same base version, compare revision numbers
            r1_match = re.search(r'\+r(\d+)', ver1)
            r2_match = re.search(r'\+r(\d+)', ver2)
            
            if r1_match and r2_match:
                r1 = int(r1_match.group(1))
                r2 = int(r2_match.group(1))
                return -1 if r1 < r2 else (1 if r1 > r2 else 0)
            elif r1_match and not r2_match:
                return 1  # Git revision is newer than release
            elif not r1_match and r2_match:
                return -1  # Release is older than git revision
            
            return 0
        except Exception:
            return -1 if ver1 < ver2 else (1 if ver1 > ver2 else 0)

def is_version_newer(current_version: str, target_version: str) -> bool:
    """Return True if target_version is newer than current_version"""
    return ArchVersionComparator.is_newer(current_version, target_version)

def compare_arch_versions(version1: str, version2: str) -> int:
    """Compare two Arch Linux version strings using proper semantics"""
    return ArchVersionComparator.compare(version1, version2)

def parse_pkgbuild_deps(pkgbuild_path):
    """
    Extract dependencies from PKGBUILD file using bash sourcing.
    
    Args:
        pkgbuild_path: Path to PKGBUILD file
        
    Returns:
        dict: Dictionary with depends, makedepends, checkdepends, provides lists
    """
    deps = {'depends': [], 'makedepends': [], 'checkdepends': [], 'provides': []}
    
    try:
        pkg_dir = pkgbuild_path.parent
        
        # Create a temporary script to source PKGBUILD and extract dependencies
        import shlex
        temp_script = f"""#!/bin/bash
cd {shlex.quote(str(pkg_dir))}
source PKGBUILD 2>/dev/null || exit 1
echo "DEPENDS_START"
printf '%s\\n' "${{depends[@]}}"
echo "DEPENDS_END"
echo "MAKEDEPENDS_START"
printf '%s\\n' "${{makedepends[@]}}"
echo "MAKEDEPENDS_END"
echo "CHECKDEPENDS_START"
printf '%s\\n' "${{checkdepends[@]}}"
echo "CHECKDEPENDS_END"
echo "PROVIDES_START"
printf '%s\\n' "${{provides[@]}}"
echo "PROVIDES_END"
"""
        
        result = subprocess.run(['bash', '-c', temp_script], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            output = result.stdout
            current_section = None
            
            for line in output.split('\n'):
                line = line.strip()
                if line == "DEPENDS_START":
                    current_section = "depends"
                elif line == "DEPENDS_END":
                    current_section = None
                elif line == "MAKEDEPENDS_START":
                    current_section = "makedepends"
                elif line == "MAKEDEPENDS_END":
                    current_section = None
                elif line == "CHECKDEPENDS_START":
                    current_section = "checkdepends"
                elif line == "CHECKDEPENDS_END":
                    current_section = None
                elif line == "PROVIDES_START":
                    current_section = "provides"
                elif line == "PROVIDES_END":
                    current_section = None
                elif line and current_section:
                    deps[current_section].append(line)
        else:
            print(f"Warning: Failed to parse PKGBUILD with bash: {result.stderr}")
        
        # Also parse dependencies from package() function by reading the file directly
        try:
            with open(pkgbuild_path, 'r') as f:
                content = f.read()
                
            # Look for depends= inside package() function
            import re
            package_func_match = re.search(r'package\(\)\s*\{(.*?)\n\}', content, re.DOTALL)
            if package_func_match:
                package_content = package_func_match.group(1)
                depends_match = re.search(r"depends=\([^)]+\)", package_content)
                if depends_match:
                    depends_line = depends_match.group(0)
                    # Extract dependencies from the line
                    deps_in_parens = re.findall(r"'([^']+)'", depends_line)
                    for dep in deps_in_parens:
                        if dep not in deps['depends']:
                            deps['depends'].append(dep)
        except Exception as e:
            pass  # Ignore errors in text parsing
            
    except subprocess.TimeoutExpired:
        print("Warning: PKGBUILD parsing timed out")
    except Exception as e:
        print(f"Warning: Error parsing PKGBUILD: {e}")
    
    return deps

def parse_database_file(db_filename, include_any=False):
    """Parse a pacman database file and return packages"""
    packages = {}
    
    try:
        with tarfile.open(db_filename, 'r:gz') as tar:
            for member in tar.getmembers():
                if member.name.endswith('/desc'):
                    desc_content = tar.extractfile(member).read().decode('utf-8')
                    
                    lines = desc_content.strip().split('\n')
                    data = {}
                    current_key = None
                    
                    for line in lines:
                        if line.startswith('%') and line.endswith('%'):
                            current_key = line[1:-1]
                            data[current_key] = []
                        elif current_key and line:
                            data[current_key].append(line)
                    
                    if 'NAME' in data and 'VERSION' in data:
                        name = data['NAME'][0]
                        version = data['VERSION'][0]
                        arch = data.get('ARCH', [''])[0]
                        
                        # Skip packages with ARCH=any unless requested
                        if not include_any and arch == 'any':
                            continue
                            
                        packages[name] = {
                            'name': name,
                            'version': version,
                            'arch': arch,
                            'basename': data.get('BASE', [name])[0],
                            'depends': data.get('DEPENDS', []),
                            'makedepends': data.get('MAKEDEPENDS', []),
                            'provides': data.get('PROVIDES', []),
                            'filename': data.get('FILENAME', [''])[0],
                            'repo': 'unknown'
                        }
    except Exception as e:
        print(f"Error parsing {db_filename}: {e}")
    
    return packages

def load_database_packages(urls, arch_suffix, download=True):
    """
    Download and parse database files for given URLs in parallel.
    """
    import concurrent.futures
    
    def download_and_parse(url):
        """Download and immediately parse a database file"""
        try:
            db_filename = url.split('/')[-1].replace('.db', f'{arch_suffix}.db')
            
            if download or not Path(db_filename).exists():
                if not download:
                    print(f"Database {db_filename} not found, downloading...")
                else:
                    print(f"Downloading {db_filename}...")
                subprocess.run(["wget", "-q", "-O", db_filename, url], check=True)
            else:
                print(f"Using existing {db_filename}")
            
            repo_name = url.split('/')[-4]  # Extract 'core' or 'extra' from URL
            print(f"Parsing {db_filename}...")
            repo_packages = parse_database_file(db_filename)
            
            for name, pkg in repo_packages.items():
                pkg['repo'] = repo_name
            
            return repo_packages
                    
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to download {url}: {e}")
            return {}
        except Exception as e:
            print(f"Warning: Failed to parse database: {e}")
            return {}
    
    packages = {}
    
    # Use ThreadPoolExecutor for parallel downloads
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(urls))) as executor:
        future_to_url = {executor.submit(download_and_parse, url): url for url in urls}
        
        for future in concurrent.futures.as_completed(future_to_url):
            repo_packages = future.result()
            packages.update(repo_packages)
    
    return packages

def load_x86_64_packages(download=True, repos=None):
    """Load x86_64 packages from official repositories"""
    urls = [
        f"{X86_64_MIRROR}/core/os/x86_64/core.db",
        f"{X86_64_MIRROR}/extra/os/x86_64/extra.db"
    ]
    
    # Filter to specific repos if requested
    if repos:
        if isinstance(repos, str):
            repos = [repos]
        filtered_urls = []
        for repo in repos:
            filtered_urls.extend([url for url in urls if f'/{repo}/' in url])
        urls = filtered_urls
    
    if not download:
        print("Using existing x86_64 databases...")
    else:
        print("Downloading x86_64 databases...")
    
    return load_database_packages(urls, '_x86_64', download)

def load_target_arch_packages(download=True, urls=None):
    """Load target architecture packages from configured repositories"""
    target_arch = get_target_architecture()
    
    if urls is None:
        # Get URLs from config
        try:
            base_url = config.get('build', 'target_base_url')
            urls = [
                f"{base_url}/core/os/{target_arch}/core.db",
                f"{base_url}/extra/os/{target_arch}/extra.db"
            ]
        except configparser.NoOptionError as e:
            print(f"ERROR: Missing configuration in config.ini: {e}")
            print("Please add the following to your config.ini:")
            print("[build]")
            print("target_base_url = https://your-repo.com/arch")
            sys.exit(1)
    
    if not download:
        print(f"Using existing {target_arch} databases...")
    else:
        print(f"Downloading {target_arch} databases...")
    
    return load_database_packages(urls, f'_{target_arch}', download)

# Compatibility alias for existing code
def load_aarch64_packages(download=True, urls=None):
    """Compatibility alias - use load_target_arch_packages instead"""
    return load_target_arch_packages(download, urls)

def load_blacklist(blacklist_file):
    """
    Load blacklisted packages with wildcard support.
    
    Reads a blacklist file containing package patterns to skip.
    Supports shell-style wildcards and ignores comments/empty lines.
    
    Args:
        blacklist_file: Path to blacklist file
        
    Returns:
        list: List of blacklist patterns
    """
    if not blacklist_file or not Path(blacklist_file).exists():
        return []
    blacklist = []
    with open(blacklist_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                blacklist.append(line)
    return blacklist

def filter_blacklisted_packages(packages, blacklist):
    """Filter packages using blacklist with wildcard matching"""
    if not blacklist:
        return packages, 0
    
    filtered_packages = []
    for pkg in packages:
        is_blacklisted = False
        for pattern in blacklist:
            if fnmatch.fnmatch(pkg['name'], pattern) or fnmatch.fnmatch(pkg.get('basename', pkg['name']), pattern):
                is_blacklisted = True
                break
        if not is_blacklisted:
            filtered_packages.append(pkg)
    
    return filtered_packages, len(packages) - len(filtered_packages)


class BuildUtils:
    """
    Shared utilities for package builders.
    
    Provides common functionality needed by both regular package building
    and bootstrap toolchain building, including command execution,
    chroot management, and package uploads.
    """
    
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.logs_dir = Path(LOGS_DIR)
    
    def run_command(self, cmd, cwd=None, capture_output=False):
        """
        Unified command runner with consistent error handling and dry-run support.
        
        Executes commands with proper error handling. In dry-run mode, shows
        what would be executed without actually running commands.
        
        Args:
            cmd: Command list to execute
            cwd: Working directory for command
            capture_output: Whether to capture stdout/stderr
            
        Returns:
            CompletedProcess: Result of command execution
        """
        if self.dry_run:
            print(f"[DRY RUN] Would run: {' '.join(cmd)}")
            if cwd:
                print(f"[DRY RUN] In directory: {cwd}")
            # Return realistic output for git stash to make dry-run logic work
            if cmd[0] == "git" and len(cmd) > 1 and cmd[1] == "stash":
                return subprocess.CompletedProcess(cmd, 0, "No local changes to save", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, cwd=cwd, capture_output=capture_output, text=True, check=True)
    
    def format_dry_run(self, action, details=None):
        """Format dry-run output consistently"""
        if self.dry_run:
            print(f"[DRY RUN] {action}")
            if details:
                for detail in details:
                    print(f"[DRY RUN]   {detail}")
    
    def cleanup_old_logs(self, package_name, keep_count=None):
        """
        Keep only the most recent N log files for a package.
        
        Prevents log directory from growing indefinitely by removing
        old build logs while keeping recent ones for debugging.
        
        Args:
            package_name: Name of package to clean logs for
            keep_count: Number of recent logs to keep (default: LOG_RETENTION_COUNT)
        """
        if keep_count is None:
            keep_count = LOG_RETENTION_COUNT
            
        if not self.logs_dir.exists():
            return
        
        # Find all log files for this package
        log_pattern = f"{package_name}-*-build.log"
        log_files = list(self.logs_dir.glob(log_pattern))
        
        # Sort by modification time (newest first)
        log_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        
        # Remove old logs beyond keep_count
        for old_log in log_files[keep_count:]:
            old_log.unlink()
    
    def setup_chroot(self, chroot_path, cache_path):
        """
        Set up or create build chroot environment.
        
        Creates a clean chroot environment for building packages if it doesn't
        exist. Uses mkarchroot with custom configuration files.
        
        Args:
            chroot_path: Path where chroot should be created
            cache_path: Path for pacman cache directory
        """
        chroot_path = Path(chroot_path)
        cache_path = Path(cache_path)
        
        if self.dry_run:
            self.format_dry_run(f"Would setup chroot at {chroot_path}", [
                f"Create cache directory: {cache_path}",
                f"Create chroot with mkarchroot" if not (chroot_path / "root").exists() else f"Use existing chroot"
            ])
            return
        
        # Create cache directory
        cache_path.mkdir(parents=True, exist_ok=True)
        
        # Create chroot if it doesn't exist
        if not (chroot_path / "root").exists():
            print(f"Creating chroot at {chroot_path}")
            chroot_path.mkdir(parents=True, exist_ok=True)
            self.run_command([
                "mkarchroot", 
                "-C", "chroot-config/pacman.conf",
                "-M", "chroot-config/makepkg.conf",
                "-c", str(cache_path),
                str(chroot_path / "root"),
                "base-devel"
            ])
        else:
            print("Using existing chroot...")
    
def should_skip_package(pkg_name, blacklist):
    """Determine if package should be skipped based on blacklist"""
    import fnmatch
    return any(fnmatch.fnmatch(pkg_name, pattern) for pattern in blacklist)

def compare_bin_package_versions(provided_version, x86_version):
    """
    Compare versions for -bin packages, ignoring pkgrel differences.
    
    For -bin packages, we only care about the upstream version part,
    not the Arch packaging revision (pkgrel). This prevents unnecessary
    rebuilds when only packaging changes.
    
    Returns: True if provided_version is older than x86_version
    """
    # Extract version without pkgrel (everything before the last '-')
    def extract_version_only(version_str):
        if '-' in version_str:
            return version_str.rsplit('-', 1)[0]
        return version_str
    
    try:
        provided_base = extract_version_only(provided_version)
        x86_base = extract_version_only(x86_version)
        return is_version_newer(provided_base, x86_base)
    except Exception:
        # If version parsing fails, assume we need to rebuild
        return True

def extract_packages(db_file, repo_name):
    """Extract packages from database file with repository information"""
    packages = parse_database_file(db_file)
    for pkg in packages.values():
        pkg['repo'] = repo_name
        if pkg['name'] in packages:
            print(f"ERROR: Package '{pkg['name']}' found in multiple repositories!")
            print(f"  First: {packages[pkg['name']]['repo']} (version {packages[pkg['name']]['version']})")
            print(f"  Second: {repo_name} (version {pkg['version']})")
            exit(1)
    return packages

def find_missing_dependencies(packages, x86_packages, target_packages):
    """Find dependencies that exist in x86_64 but missing from target architecture"""
    missing_deps = set()
    
    # Build provides mapping for target architecture packages
    target_provides = {}
    for name, pkg in target_packages.items():
        target_provides[name] = pkg
        for provide in pkg['provides']:
            provide_name = provide.split('=')[0]
            target_provides[provide_name] = pkg
    
    for pkg in packages:
        all_deps = pkg['depends'] + pkg['makedepends']
        if 'checkdepends' in pkg:
            all_deps += pkg['checkdepends']
            
        for dep in all_deps:
            dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
            if dep_name in x86_packages and dep_name not in target_packages and dep_name not in target_provides:
                missing_deps.add(dep_name)
    
    return missing_deps

def load_all_packages_parallel(download=True, x86_repos=None, target_repos=None, include_any=False):
    """Load both x86_64 and target architecture packages in parallel"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    target_arch = get_target_architecture()
    
    # Build URLs
    x86_urls = [
        f"{X86_64_MIRROR}/core/os/x86_64/core.db",
        f"{X86_64_MIRROR}/extra/os/x86_64/extra.db"
    ]
    target_urls = []
    try:
        base_url = config.get('build', 'target_base_url')
        target_urls = [
            f"{base_url}/core/os/{target_arch}/core.db",
            f"{base_url}/extra/os/{target_arch}/extra.db"
        ]
    except configparser.NoOptionError as e:
        print(f"ERROR: Missing configuration in config.ini: {e}")
        print("Please add the following to your config.ini:")
        print("[build]")
        print("target_base_url = https://your-repo.com/arch")
        sys.exit(1)
    
    # Filter URLs if specific repos requested
    if x86_repos:
        if 'core' in x86_repos and 'extra' not in x86_repos:
            x86_urls = [x86_urls[0]]
        elif 'extra' in x86_repos and 'core' not in x86_repos:
            x86_urls = [x86_urls[1]]
    
    if target_repos:
        if 'core' in target_repos and 'extra' not in target_repos:
            target_urls = [target_urls[0]]
        elif 'extra' in target_repos and 'core' not in target_repos:
            target_urls = [target_urls[1]]
    
    def load_arch_packages(urls, arch_suffix, arch_name):
        packages = load_packages_with_any(urls, arch_suffix, download=download, include_any=include_any)
        print(f"Loaded {len(packages)} {arch_name} packages")
        return packages
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        x86_future = executor.submit(load_arch_packages, x86_urls, '_x86_64', 'x86_64')
        target_future = executor.submit(load_arch_packages, target_urls, f'_{target_arch}', target_arch)
        
        x86_packages = x86_future.result()
        target_packages = target_future.result()
    
    return x86_packages, target_packages

def load_packages_with_any(urls, arch_suffix, download=True, include_any=True):
    """Load packages including ARCH=any packages"""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed
    packages = {}
    
    def download_and_parse(url):
        """Download and immediately parse a database file"""
        try:
            db_filename = url.split('/')[-1].replace('.db', f'{arch_suffix}.db')
            
            if download:
                print(f"Downloading {db_filename}...")
                subprocess.run(["wget", "-q", "-O", db_filename, url], check=True)
            else:
                print(f"Using existing {db_filename}...")
            
            repo_name = url.split('/')[-4]
            print(f"Parsing {db_filename}...")
            repo_packages = parse_database_file(db_filename, include_any=include_any)
            
            # Add repo info to each package
            for name, pkg in repo_packages.items():
                pkg['repo'] = repo_name
            
            return repo_packages
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to download {url}: {e}")
            return {}
        except Exception as e:
            print(f"Warning: Failed to parse {db_filename}: {e}")
            return {}
    
    # Process all URLs in parallel
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        future_to_url = {executor.submit(download_and_parse, url): url for url in urls}
        
        for future in as_completed(future_to_url):
            repo_packages = future.result()
            packages.update(repo_packages)
    
    return packages

def import_gpg_keys():
    """Import GPG keys from keys/pgp/ directory"""
    keys_dir = Path("keys/pgp")
    if not keys_dir.exists():
        return
    
    for key_file in keys_dir.glob("*.asc"):
        try:
            print(f"Importing GPG key: {key_file.name}")
            subprocess.run(["gpg", "--import", str(key_file)], 
                         check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to import GPG key {key_file}: {e}")


def upload_packages(pkg_dir, target_repo, dry_run=False):
        """
        Upload all built packages to repository.
        
        Finds all built package files in the directory and uploads them
        to the specified repository using the repo-upload tool.
        
        Args:
            pkg_dir: Directory containing built packages
            target_repo: Target repository name (e.g., 'core-testing')
            
        Returns:
            int: Number of packages uploaded
        """
        built_packages = [str(f) for f in pkg_dir.glob("*.pkg.tar.*") if not f.name.endswith('.sig')]
        
        if not built_packages:
            print(f"ERROR: No packages found to upload in {pkg_dir}")
            sys.exit(1)
        
        for pkg in built_packages:
            try:
                cmd = [
                    "repo-upload", pkg,
                    "--arch", get_target_architecture(),
                    "--repo", target_repo,
                    "--bucket", UPLOAD_BUCKET
                ]
                if dry_run:
                    print(f"Would run: {' '.join(cmd)}")
                else:
                    subprocess.run(cmd, check=True)
                    print(f"Uploaded {Path(pkg).name} to {target_repo}")
            except subprocess.CalledProcessError as e:
                print(f"ERROR: Failed to upload {Path(pkg).name}: {e}")
                sys.exit(1)
        
        return len(built_packages)
