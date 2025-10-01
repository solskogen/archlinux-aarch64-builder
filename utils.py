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
from pathlib import Path
from packaging import version

# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

# Configuration constants
BUILD_ROOT = config.get('build', 'build_root', fallback='/scratch/builder')
CACHE_PATH = f"{BUILD_ROOT}/pacman-cache"
UPLOAD_BUCKET = config.get('build', 'upload_bucket', fallback='arch-linux-repos.drzee.net')
X86_64_MIRROR = config.get('build', 'x86_64_mirror', fallback='https://geo.mirror.pkgbuild.com')
LOG_RETENTION_COUNT = 3

# Constants
PACKAGE_SKIP_FLAG = 1

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

def is_version_newer(current_version: str, target_version: str) -> bool:
    """Return True if target_version is newer than current_version"""
    return compare_arch_versions(current_version, target_version) < 0

def compare_arch_versions(version1: str, version2: str) -> int:
    """
    Compare two Arch Linux version strings using proper semantics.
    
    Handles Arch Linux specific version formats including:
    - Epoch versions (1:2.0-1)
    - Git revision versions (1.0+r123.abc123-1)  
    - Standard semantic versions (1.2.3-1)
    
    Args:
        version1: First version string
        version2: Second version string
        
    Returns:
        int: -1 if version1 < version2, 0 if equal, 1 if version1 > version2
    """
    epoch1, ver1 = split_epoch_version(version1)
    epoch2, ver2 = split_epoch_version(version2)
    
    # Compare epochs first
    if epoch1 != epoch2:
        return epoch1 - epoch2
    
    # Handle git revision versions
    if has_git_revision(ver1) or has_git_revision(ver2):
        return compare_git_versions(ver1, ver2)
    
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

def split_epoch_version(version_str: str) -> tuple:
    """Split version string into epoch and version parts"""
    if ':' in version_str:
        epoch_str, ver_str = version_str.split(':', 1)
        return int(epoch_str), ver_str
    return 0, version_str

def has_git_revision(version_str: str) -> bool:
    """Check if version contains git revision marker"""
    return '+r' in version_str

def compare_git_versions(ver1: str, ver2: str) -> int:
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
        temp_script = f"""#!/bin/bash
cd "{pkg_dir}"
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
                            'basename': data.get('BASE', [name])[0],
                            'depends': data.get('DEPENDS', []),
                            'makedepends': data.get('MAKEDEPENDS', []),
                            'provides': data.get('PROVIDES', []),
                            'repo': 'unknown'
                        }
    except Exception as e:
        print(f"Error parsing {db_filename}: {e}")
    
    return packages

def load_database_packages(urls, arch_suffix, download=True):
    """
    Download and parse database files for given URLs.
    
    Downloads pacman database files and extracts package information.
    Filters out ARCH=any packages since they don't need rebuilding.
    
    Args:
        urls: List of database URLs to download
        arch_suffix: Suffix for local filename (e.g., '_x86_64', '_aarch64')
        download: Whether to download files (False = use existing files)
    
    Returns:
        dict: Package name -> package data mapping
    """
    def download_and_parse(url, packages_dict, lock):
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
            
            with lock:
                for name, pkg in repo_packages.items():
                    pkg['repo'] = repo_name
                    packages_dict[name] = pkg
                    
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to download {url}: {e}")
        except Exception as e:
            print(f"Warning: Failed to parse database: {e}")
    
    packages = {}
    lock = threading.Lock()
    threads = []
    
    # Start download+parse threads
    for url in urls:
        thread = threading.Thread(target=download_and_parse, args=(url, packages, lock))
        thread.start()
        threads.append(thread)
    
    # Wait for all to complete
    for thread in threads:
        thread.join()
    
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

def load_aarch64_packages(download=True, urls=None):
    """Load AArch64 packages from configured repositories"""
    if urls is None:
        urls = [
            "https://arch-linux-repo.drzee.net/arch/core/os/aarch64/core.db",
            "https://arch-linux-repo.drzee.net/arch/extra/os/aarch64/extra.db"
        ]
    
    if not download:
        print("Using existing AArch64 databases...")
    else:
        print("Downloading AArch64 databases...")
    
    return load_database_packages(urls, '_aarch64', download)

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
        self.logs_dir = Path("logs")
    
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
    
    def upload_packages(self, pkg_dir, target_repo):
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
                self.run_command([
                    "repo-upload", pkg,
                    "--arch", "aarch64",
                    "--repo", target_repo,
                    "--bucket", UPLOAD_BUCKET
                ])
                print(f"Uploaded {Path(pkg).name} to {target_repo}")
            except subprocess.CalledProcessError as e:
                print(f"ERROR: Failed to upload {Path(pkg).name}: {e}")
                sys.exit(1)
        
        return len(built_packages)
