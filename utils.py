#!/usr/bin/env python3
"""
Shared utility functions for the Arch Linux AArch64 build system.

This module provides common functionality used across multiple scripts:
- Package name validation and security
- Version comparison with Arch Linux semantics  
- Database loading and parsing
- Blacklist management
- Path safety utilities

The utilities handle various edge cases in Arch Linux package management
including epoch versions, git revisions, and architecture filtering.
"""

import os
import fnmatch
import subprocess
import re
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional
from packaging import version

# Constants
PACKAGE_SKIP_FLAG = 1
PACKAGE_BUILD_FLAG = 0

class Architecture(Enum):
    ANY = 'any'
    X86_64 = 'x86_64'
    AARCH64 = 'aarch64'

class Repository(Enum):
    CORE = 'core'
    EXTRA = 'extra'
    AUR = 'aur'
    LOCAL = 'local'

@dataclass
class BuildConfig:
    """Configuration for the build system"""
    upstream_core_url: str = "https://geo.mirror.pkgbuild.com/core/os/x86_64/core.db"
    upstream_extra_url: str = "https://geo.mirror.pkgbuild.com/extra/os/x86_64/extra.db"
    target_core_url: str = "https://arch-linux-repo.drzee.net/arch/core/os/aarch64/core.db"
    target_extra_url: str = "https://arch-linux-repo.drzee.net/arch/extra/os/aarch64/extra.db"
    build_root: Path = Path("/var/tmp/builder")
    cache_path: Path = Path("/var/tmp/builder/pacman-cache")

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
    import tarfile
    
    def parse_database_file(db_filename):
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
                            # Skip packages with ARCH=any
                            if data.get('ARCH', [''])[0] == 'any':
                                continue
                                
                            name = data['NAME'][0]
                            version = data['VERSION'][0]
                            
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
    
    packages = {}
    
    for url in urls:
        try:
            db_filename = url.split('/')[-1].replace('.db', f'{arch_suffix}.db')
            
            if download or not Path(db_filename).exists():
                if not download:
                    print(f"Database {db_filename} not found, downloading...")
                else:
                    print(f"Downloading {db_filename}...")
                subprocess.run(["wget", "-q", "-O", db_filename, url], check=True)
            
            repo_name = url.split('/')[-4]  # Extract 'core' or 'extra' from URL
            print(f"Parsing {db_filename}...")
            repo_packages = parse_database_file(db_filename)
            
            for name, pkg in repo_packages.items():
                pkg['repo'] = repo_name
                packages[name] = pkg
                
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to download {url}: {e}")
        except Exception as e:
            print(f"Warning: Failed to parse {db_filename}: {e}")
    
    return packages

def load_x86_64_packages(download=True, repos=None):
    """Load x86_64 packages from official repositories"""
    urls = [
        "https://geo.mirror.pkgbuild.com/core/os/x86_64/core.db",
        "https://geo.mirror.pkgbuild.com/extra/os/x86_64/extra.db"
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
