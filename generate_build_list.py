#!/bin/bash
'''exec' python3 -B "$0" "$@"
' '''
"""
Arch Linux AArch64 Build List Generator

This script compares package versions between x86_64 and AArch64 repositories
to identify packages that need building. It generates a dependency-ordered
build list with complete package metadata.

Key features:
- Version comparison using Arch Linux state repository
- Dependency resolution and topological sorting
- PKGBUILD fetching with git tag management
- Variable expansion in PKGBUILDs
- Blacklist filtering and missing package detection
- Support for AUR and local packages

Usage:
    ./generate_build_list.py                    # Find outdated packages
    ./generate_build_list.py --packages vim    # Force rebuild specific packages
    ./generate_build_list.py --missing-packages # List missing packages
"""

import urllib.request
import tarfile
import json
import os
import argparse
import fnmatch
import sys
import datetime
import subprocess
import re
import shutil
import tempfile
from pathlib import Path
from packaging import version
from utils import (
    load_blacklist, load_x86_64_packages, load_aarch64_packages,
    validate_package_name, safe_path_join, is_version_newer,
    PACKAGE_SKIP_FLAG, PACKAGE_BUILD_FLAG
)



def get_provides_mapping():
    """
    Get basename to provides mapping from upstream x86_64 databases.
    
    Downloads and parses official Arch Linux databases to build a mapping
    of package names to what they provide. This is used for dependency
    resolution when packages depend on virtual packages.
    
    Returns:
        dict: Mapping of provided names to providing package basenames
    """
    mirror_url = 'https://archlinux.carebears.no'
    provides_map = {}
    
    for repo in ['core', 'extra']:
        db_url = f"{mirror_url}/{repo}/os/x86_64/{repo}.db"
        
        try:
            with tempfile.NamedTemporaryFile() as tmp_file:
                urllib.request.urlretrieve(db_url, tmp_file.name)
                
                with tarfile.open(tmp_file.name, 'r:gz') as tar:
                    for member in tar.getmembers():
                        if member.name.endswith('/desc'):
                            desc_content = tar.extractfile(member).read().decode('utf-8')
                            
                            # Parse desc file
                            sections = desc_content.strip().split('\n\n')
                            pkg_name = ''
                            pkg_base = ''
                            pkg_provides = []
                            
                            for section in sections:
                                lines = section.strip().split('\n')
                                if not lines:
                                    continue
                                    
                                section_name = lines[0].strip('%')
                                section_content = lines[1:]
                                
                                if section_name == 'NAME' and section_content:
                                    pkg_name = section_content[0]
                                elif section_name == 'BASE' and section_content:
                                    pkg_base = section_content[0]
                                elif section_name == 'PROVIDES':
                                    pkg_provides = section_content
                            
                            # Use base if available, otherwise use name
                            basename = pkg_base or pkg_name
                            if basename:
                                provides_map[basename] = basename  # Package provides itself
                                for provide in pkg_provides:
                                    provide_name = provide.split('=')[0]
                                    provides_map[provide_name] = basename
        except Exception as e:
            print(f"Warning: failed to download {repo}.db: {e}")
    
    return provides_map

def write_results(packages, args):
    """Write results to packages_to_build.json"""
    print("Writing results to packages_to_build.json...")
    output_data = {
        "_command": " ".join(sys.argv),
        "_timestamp": datetime.datetime.now().isoformat(),
        "packages": packages
    }
    with open("packages_to_build.json", "w") as f:
        json.dump(output_data, f, indent=2)
    
    print(f"\nBuild Statistics:")
    print(f"Total packages to build: {len(packages)}")
    if packages:
        stages = {}
        for pkg in packages:
            stage = pkg.get('build_stage', 0)
            stages[stage] = stages.get(stage, 0) + 1
        print(f"Total build stages: {max(stages.keys()) + 1 if stages else 0}")
        print("Packages per stage:")
        for stage in sorted(stages.keys()):
            print(f"  Stage {stage}: {stages[stage]} packages")

def parse_database_file(db_filename):
    """Parse a pacman database file and return packages"""
    import tarfile
    packages = {}
    
    try:
        with tarfile.open(db_filename, 'r:gz') as tar:
            for member in tar.getmembers():
                if member.name.endswith('/desc'):
                    desc_content = tar.extractfile(member).read().decode('utf-8')
                    
                    # Parse desc content manually (don't filter ARCH=any for ARM packages)
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
                            'repo': 'unknown'  # Will be set by caller
                        }
    except Exception as e:
        print(f"Error parsing {db_filename}: {e}")
    
    return packages

def load_state_packages():
    """Load x86_64 packages from state repository with consistency checking"""
    update_state_repo()
    return _load_state_packages_no_update()

def _load_state_packages_no_update():
    """Load x86_64 packages from existing state repository without updating"""
    
    x86_packages = {}
    package_basenames = {}
    
    for repo in ["core", "extra"]:
        packages = parse_state_repo(repo)
        
        # Check for basenames that exist in multiple repositories (this indicates an error)
        for pkg_name, pkg_data in packages.items():
            basename = pkg_data['basename']
            if basename in package_basenames:
                print(f"ERROR: Package basename '{basename}' found in both {package_basenames[basename]} and {repo} repositories")
                sys.exit(1)
            package_basenames[basename] = repo
        
        x86_packages.update(packages)
    
    return x86_packages

def update_state_repo():
    """Update the state repository to get latest package versions"""
    state_dir = Path("state")
    if not state_dir.exists():
        print("Cloning state repository...")
        subprocess.run(["git", "clone", "https://gitlab.archlinux.org/archlinux/packaging/state"], check=True, stdout=subprocess.DEVNULL)
    else:
        print("Updating state repository...")
        try:
            subprocess.run(["git", "pull"], cwd=state_dir, check=True, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            print("State repository corrupted, re-cloning...")
            shutil.rmtree(state_dir)
            subprocess.run(["git", "clone", "https://gitlab.archlinux.org/archlinux/packaging/state"], check=True, stdout=subprocess.DEVNULL)

def parse_state_repo(repo_name):
    """Parse packages from state repository - only core-x86_64 and extra-x86_64"""
    state_dir = Path("state") / f"{repo_name}-x86_64"
    packages = {}
    
    if not state_dir.exists():
        print(f"Warning: {state_dir} not found in state repository")
        return packages
    
    # Get all files at once and process in batch
    pkg_files = [f for f in state_dir.iterdir() if f.is_file()]
    total_files = len(pkg_files)
    
    print(f"Parsing {total_files} packages from {repo_name}...")
    
    for i, pkg_file in enumerate(pkg_files, 1):
        if i % 1000 == 0 or i == total_files:
            print(f"  Progress: {i}/{total_files} ({i*100//total_files}%)")
        
        try:
            line = pkg_file.read_text().strip()
            parts = line.split()
            if len(parts) >= 2:
                name = pkg_file.name
                version = parts[1]  # Use second field (version)
                packages[name] = {
                    'name': name,
                    'basename': name,
                    'version': version,
                    'depends': [],
                    'makedepends': [],
                    'provides': [],
                    'repo': repo_name
                }
        except Exception as e:
            print(f"Warning: Failed to parse {pkg_file}: {e}")
    
    return packages

def parse_pkgbuild_deps(pkgbuild_path):
    """
    Extract dependencies from PKGBUILD file with robust parsing.
    
    Handles various PKGBUILD formats including:
    - Single and multi-line dependency arrays
    - Comments within arrays (# comment)
    - Variable expansion ($_variable)
    - Inline comments after dependencies
    - Multiple quoted items on same line
    
    Args:
        pkgbuild_path: Path to PKGBUILD file
        
    Returns:
        dict: Dictionary with depends, makedepends, checkdepends, provides lists
    """
    deps = {'depends': [], 'makedepends': [], 'checkdepends': [], 'provides': []}
    
    try:
        with open(pkgbuild_path, 'r') as f:
            content = f.read()
        
        # Parse dependency arrays more robustly
        for dep_type in ['depends', 'makedepends', 'checkdepends', 'provides']:
            # Match array declarations including multi-line
            pattern = rf'^{dep_type}=\s*\('
            lines = content.split('\n')
            in_array = False
            items = []
            
            for line in lines:
                line_stripped = line.strip()
                
                if re.match(pattern, line_stripped):
                    in_array = True
                    # Check if it's a single-line array
                    if ')' in line:
                        # Single line array
                        array_content = re.search(rf'{dep_type}=\s*\((.*?)\)', line).group(1)
                        # Handle inline comments
                        if '#' in array_content:
                            array_content = array_content.split('#')[0].strip()
                        
                        # Parse multiple quoted items
                        import shlex
                        try:
                            line_items = shlex.split(array_content)
                            for item in line_items:
                                if item and item not in items:  # Avoid duplicates
                                    items.append(item)
                        except ValueError:
                            # Fallback if shlex fails
                            for item in array_content.split():
                                item = item.strip().strip('\'"')
                                if item and item not in items:
                                    items.append(item)
                        in_array = False
                    else:
                        # Multi-line array - check if there's content after the opening parenthesis
                        remaining = line_stripped.split('(', 1)[1] if '(' in line_stripped else ''
                        if remaining.strip():
                            # Handle inline comments
                            if '#' in remaining:
                                remaining = remaining.split('#')[0].strip()
                            
                            # Parse multiple quoted items
                            import shlex
                            try:
                                line_items = shlex.split(remaining)
                                for item in line_items:
                                    if item and item not in items:  # Avoid duplicates
                                        items.append(item)
                            except ValueError:
                                # Fallback if shlex fails
                                item = remaining.strip('\'"')
                                if item and item not in items:
                                    items.append(item)
                    continue
                
                if in_array:
                    if ')' in line_stripped:
                        # End of multi-line array
                        remaining = line_stripped.split(')')[0]
                        if remaining.strip():
                            # Handle inline comments
                            if '#' in remaining:
                                remaining = remaining.split('#')[0].strip()
                            
                            # Parse multiple quoted items
                            import shlex
                            try:
                                line_items = shlex.split(remaining)
                                for item in line_items:
                                    if item and item not in items:  # Avoid duplicates
                                        items.append(item)
                            except ValueError:
                                # Fallback if shlex fails
                                item = remaining.strip().strip('\'"')
                                if item and item not in items:
                                    items.append(item)
                        in_array = False
                    else:
                        # Middle of multi-line array
                        if line_stripped and not line_stripped.startswith('#'):
                            # Handle inline comments - only remove comment part, keep dependency name
                            clean_line = line_stripped
                            if '#' in clean_line:
                                clean_line = clean_line.split('#')[0].strip()
                            
                            # Parse multiple quoted items on same line
                            import shlex
                            try:
                                line_items = shlex.split(clean_line)
                                for item in line_items:
                                    if item and item not in items:  # Avoid duplicates
                                        items.append(item)
                            except ValueError:
                                # Fallback if shlex fails
                                item = clean_line.strip('\'"')
                                if item and item not in items:
                                    items.append(item)
            
            deps[dep_type] = items
    except Exception as e:
        print(f"Warning: Failed to parse PKGBUILD {pkgbuild_path}: {e}")
    
    return deps

def fetch_pkgbuild_deps(packages_to_build, no_update=False):
    """
    Fetch PKGBUILDs for packages and extract complete dependency information.
    
    This function:
    1. Clones or updates git repositories for each package
    2. Handles version tag checkout for specific versions
    3. Parses PKGBUILDs to extract all dependencies
    4. Filters dependencies to only include packages in the build list
    5. Provides clear progress messages about git operations
    
    Args:
        packages_to_build: List of package dictionaries to process
        no_update: Skip git operations, use existing PKGBUILDs
        
    Returns:
        list: Updated package list with complete dependency information
    """
    if not packages_to_build:
        return packages_to_build
        
    # Check if required tools are available
    try:
        subprocess.run(["pkgctl", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: pkgctl not found. Please install devtools package.")
        sys.exit(1)
    
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: git not found. Please install git package.")
        sys.exit(1)
    
    Path("pkgbuilds").mkdir(exist_ok=True)
    
    # Filter out blacklisted packages for PKGBUILD fetching
    packages_to_fetch = [pkg for pkg in packages_to_build if pkg.get('skip', 0) != 1]
    blacklisted_packages = [pkg for pkg in packages_to_build if pkg.get('skip', 0) == 1]
    
    total = len(packages_to_fetch)
    for i, pkg in enumerate(packages_to_fetch, 1):
        name = pkg['name']
        basename = pkg.get('basename', name)  # Use basename for git operations
        pkgbuild_dir = Path("pkgbuilds") / basename
        pkgbuild_path = pkgbuild_dir / "PKGBUILD"
        
        # Check if PKGBUILD exists and get current version
        current_version = None
        if pkgbuild_path.exists():
            try:
                with open(pkgbuild_path, 'r') as f:
                    content = f.read()
                    # Extract pkgver and pkgrel - only if they don't contain complex variables
                    pkgver_match = re.search(r'^pkgver=(.+)$', content, re.MULTILINE)
                    pkgrel_match = re.search(r'^pkgrel=(.+)$', content, re.MULTILINE)
                    if pkgver_match and pkgrel_match:
                        pkgver = pkgver_match.group(1).strip('\'"')
                        pkgrel = pkgrel_match.group(1).strip('\'"')
                        # Only use if no complex variable substitution
                        if not ('${' in pkgver or '$(' in pkgver):
                            current_version = f"{pkgver}-{pkgrel}"
            except Exception:
                pass  # If we can't read version, treat as if PKGBUILD doesn't exist
        
        # Determine what action to take based on target version vs database version
        target_version = pkg['version']
        needs_update = current_version != target_version
        
        # Fetch or update PKGBUILD (skip if no_update is True)
        if no_update:
            print(f"[{i}/{total}] Processing {name} (no update)...")
        elif not pkgbuild_path.exists():
            # Need to clone the repository
            try:
                if pkg.get('use_aur', False):
                    print(f"[{i}/{total}] Processing {name} (fetching from AUR)...")
                    result = subprocess.run(["git", "clone", f"https://aur.archlinux.org/{basename}.git", basename], 
                                         cwd="pkgbuilds", check=True, 
                                         capture_output=True, text=True)
                else:
                    print(f"[{i}/{total}] Processing {name} (fetching {target_version})...")
                    # Use git directly for more reliable tag switching
                    # Convert ++ to plusplus for GitLab URLs
                    gitlab_basename = basename.replace('++', 'plusplus')
                    if pkg.get('force_latest', False):
                        # Clone and stay on main branch
                        result = subprocess.run(["git", "clone", f"https://gitlab.archlinux.org/archlinux/packaging/packages/{gitlab_basename}.git", basename], 
                                             cwd="pkgbuilds", check=True, 
                                             capture_output=True, text=True)
                    else:
                        # Clone and checkout specific version tag
                        version_tag = pkg['version']
                        result = subprocess.run(["git", "clone", f"https://gitlab.archlinux.org/archlinux/packaging/packages/{gitlab_basename}.git", basename], 
                                             cwd="pkgbuilds", check=True, 
                                             capture_output=True, text=True)
                        # Checkout the specific tag - try different tag formats
                        git_version_tag = version_tag.replace(':', '-')
                        tag_formats = [git_version_tag, version_tag, f"v{git_version_tag}", f"{basename}-{git_version_tag}"]
                        checkout_success = False
                        for tag_format in tag_formats:
                            try:
                                subprocess.run(["git", "checkout", tag_format], 
                                             cwd=f"pkgbuilds/{basename}", check=True, 
                                             capture_output=True, text=True)
                                checkout_success = True
                                break
                            except subprocess.CalledProcessError:
                                continue
                        
                        if not checkout_success:
                            print(f"Warning: Could not find tag for {basename} version {version_tag}, using latest commit")
                            subprocess.run(["git", "pull"], cwd=f"pkgbuilds/{basename}", check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.strip() if e.stderr else 'Unknown error'
                print(f"Warning: Failed to fetch PKGBUILD for {name} (basename: {basename}): {error_msg}")
                if "Username for" in error_msg or "Authentication failed" in error_msg:
                    print(f"  -> Package {basename} may not exist in official repositories or requires authentication")
                continue
        elif needs_update or pkg.get('force_latest', False):
            # Repository exists but needs update
            pkg_repo_dir = Path("pkgbuilds") / basename
            try:
                if pkg.get('force_latest', False):
                    print(f"[{i}/{total}] Processing {name} (updating to latest commit)...")
                    subprocess.run(["git", "pull"], cwd=pkg_repo_dir, check=True, capture_output=True)
                else:
                    if current_version:
                        print(f"[{i}/{total}] Processing {name} (updating {current_version} -> {target_version})...")
                    else:
                        print(f"[{i}/{total}] Processing {name} (updating to {target_version})...")
                    subprocess.run(["git", "fetch", "--tags"], cwd=pkg_repo_dir, check=True, capture_output=True)
                    # Try different tag formats - replace : with - for git tags
                    git_version_tag = target_version.replace(':', '-')
                    tag_formats = [git_version_tag, target_version, f"v{git_version_tag}", f"{basename}-{git_version_tag}"]
                    checkout_success = False
                    for tag_format in tag_formats:
                        try:
                            result = subprocess.run(["git", "checkout", tag_format], cwd=pkg_repo_dir, check=True, capture_output=True, text=True)
                            checkout_success = True
                            break
                        except subprocess.CalledProcessError as checkout_error:
                            # Debug: show what tag format failed
                            if tag_format == tag_formats[-1]:  # Last attempt
                                print(f"Debug: All tag formats failed for {basename}. Tried: {tag_formats}")
                                if checkout_error.stderr:
                                    print(f"Last error: {checkout_error.stderr.strip()}")
                            continue
                    
                    if not checkout_success:
                        print(f"Warning: Could not find tag for {basename} version {target_version}, using latest commit")
                        try:
                            # Reset to clean state before pulling
                            subprocess.run(["git", "reset", "--hard"], cwd=pkg_repo_dir, check=True, capture_output=True)
                            subprocess.run(["git", "pull"], cwd=pkg_repo_dir, check=True, capture_output=True, text=True)
                        except subprocess.CalledProcessError as pull_error:
                            print(f"Warning: Failed to pull latest commit for {basename}: {pull_error}")
                            if pull_error.stderr:
                                print(f"Git error: {pull_error.stderr.strip()}")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to update {basename}: {e}")
                if hasattr(e, 'stderr') and e.stderr:
                    print(f"Git error: {e.stderr.strip()}")
        else:
            # Repository exists and is up to date
            print(f"[{i}/{total}] Processing {name} (already up to date: {current_version or target_version})...")
        
        # Parse dependencies from existing PKGBUILD
        if pkgbuild_path.exists():
            deps = parse_pkgbuild_deps(pkgbuild_path)
            pkg.update(deps)
    
    # Return combined list with blacklisted packages (unchanged) and fetched packages (with updated deps)
    all_packages = packages_to_fetch + blacklisted_packages
    
    # Get provides mapping from upstream x86_64 databases
    print("Loading provides mapping from upstream databases...")
    provides_map = get_provides_mapping()
    
    # Clean up dependencies - only keep deps that are in the build list
    build_list_names = {pkg['name'] for pkg in all_packages}
    build_list_basenames = {pkg.get('basename', pkg['name']) for pkg in all_packages}
    
    for pkg in all_packages:
        for dep_type in ['depends', 'makedepends', 'checkdepends']:
            if dep_type in pkg:
                cleaned_deps = []
                
                for dep in pkg[dep_type]:
                    dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                    # Keep dependency if it's in the build list (by name or basename)
                    if dep_name in build_list_names or dep_name in build_list_basenames:
                        cleaned_deps.append(dep)
                    # Also check if .so dependency resolves to a package in build list
                    elif dep_name in provides_map:
                        providing_basename = provides_map[dep_name]
                        if providing_basename in build_list_basenames:
                            cleaned_deps.append(dep)
                
                pkg[dep_type] = cleaned_deps
    
    return all_packages

def download_and_parse_db(url, arch_suffix, repo_name, packages_dict):
    """Download and immediately parse a database file"""
    filename = url.split('/')[-1].replace('.db', f'_{arch_suffix}.db')
    
    if os.path.exists(filename):
        print(f"Using existing {filename}")
    else:
        print(f"Downloading {filename}...")
        urllib.request.urlretrieve(url, filename)
    
    print(f"Parsing {filename}...")
    repo_packages = extract_packages(filename, repo_name)
    packages_dict.update(repo_packages)
    return filename

def download_dbs(x86_urls, arm_urls, skip_if_exists=False, skip_x86=False):
    x86_files = []
    arm_files = []
    
    if not skip_x86:
        for url in x86_urls:
            filename = url.split('/')[-1].replace('.db', '_x86_64.db')
            if skip_if_exists and os.path.exists(filename):
                print(f"Using existing {filename}")
            else:
                urllib.request.urlretrieve(url, filename)
            x86_files.append(filename)
    
    for url in arm_urls:
        filename = url.split('/')[-1].replace('.db', '_aarch64.db')
        if skip_if_exists and os.path.exists(filename):
            print(f"Using existing {filename}")
        else:
            urllib.request.urlretrieve(url, filename)
        arm_files.append(filename)
    
    return x86_files, arm_files

def parse_desc(content):
    lines = content.strip().split('\n')
    data = {}
    current_key = None
    
    for line in lines:
        if line.startswith('%') and line.endswith('%'):
            current_key = line[1:-1]
            data[current_key] = []
        elif current_key and line:
            data[current_key].append(line)
    
    # Skip packages with ARCH=any
    if data.get('ARCH', [''])[0] == 'any':
        return None
    
    basename = data.get('BASE', [''])[0] or data.get('NAME', [''])[0]
    version_str = data.get('VERSION', [''])[0]
    
    return {
        'name': data.get('NAME', [''])[0],
        'basename': basename,
        'version': version_str,
        'depends': data.get('DEPENDS', []),
        'makedepends': data.get('MAKEDEPENDS', []),
        'provides': data.get('PROVIDES', [])
    }

def extract_packages(db_file, repo_name):
    packages = {}
    with tarfile.open(db_file, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith('/desc'):
                desc_file = tar.extractfile(member)
                content = desc_file.read().decode('utf-8')
                pkg_data = parse_desc(content)
                if pkg_data:  # Skip None (ARCH=any packages)
                    pkg_data['repo'] = repo_name
                    if pkg_data['name'] in packages:
                        print(f"ERROR: Package '{pkg_data['name']}' found in multiple repositories!")
                        print(f"  First: {packages[pkg_data['name']]['repo']} (version {packages[pkg_data['name']]['version']})")
                        print(f"  Second: {repo_name} (version {pkg_data['version']})")
                        exit(1)
                    packages[pkg_data['name']] = pkg_data
    return packages

def find_missing_dependencies(packages, x86_packages, arm_packages):
    """Find dependencies that exist in x86_64 but missing from aarch64"""
    missing_deps = set()
    
    # Build provides mapping for ARM packages
    arm_provides = {}
    for name, pkg in arm_packages.items():
        arm_provides[name] = pkg
        for provide in pkg['provides']:
            provide_name = provide.split('=')[0]
            arm_provides[provide_name] = pkg
    
    for pkg in packages:
        all_deps = pkg['depends'] + pkg['makedepends']
        if 'checkdepends' in pkg:
            all_deps += pkg['checkdepends']
            
        for dep in all_deps:
            dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
            if dep_name in x86_packages and dep_name not in arm_packages and dep_name not in arm_provides:
                missing_deps.add(dep_name)
    
    return missing_deps

def compare_versions(x86_packages, arm_packages, force_packages=None, blacklist=None, missing_packages_mode=False, aur_packages=None, use_latest=False):
    """
    Compare package versions between x86_64 and AArch64 repositories.
    
    Identifies packages that need building by comparing versions and handling
    various scenarios like missing packages, blacklisted packages, and
    bootstrap-only packages.
    
    Args:
        x86_packages: Dictionary of x86_64 packages
        arm_packages: Dictionary of AArch64 packages  
        force_packages: Set of packages to force rebuild
        blacklist: List of blacklist patterns
        missing_packages_mode: Only return missing packages
        aur_packages: Set of packages to get from AUR
        use_latest: Use latest git commits instead of version tags
        
    Returns:
        tuple: (packages_to_build, skipped_packages, blacklisted_missing)
    """
    force_packages = set(force_packages or [])
    aur_packages = set(aur_packages or [])
    blacklist = blacklist or []
    
    # Bootstrap-only packages (excluded from normal builds but not from bootstrap detection)
    bootstrap_only = {'linux-api-headers', 'glibc', 'binutils', 'gcc'}
    
    skipped_packages = []
    blacklisted_missing = []
    
    # Group packages by basename
    x86_bases = {}
    arm_bases = {}
    
    # Build provides mapping for ARM packages (only if needed for missing deps)
    arm_provides = {name: pkg for name, pkg in arm_packages.items()}
    if not missing_packages_mode:
        for pkg in arm_packages.values():
            for provide in pkg['provides']:
                provide_name = provide.split('=')[0]
                arm_provides[provide_name] = pkg
    
    for name, pkg in x86_packages.items():
        basename = pkg['basename']
        if basename not in x86_bases:
            x86_bases[basename] = {'packages': [], 'version': pkg['version'], 'pkg_data': pkg}
        x86_bases[basename]['packages'].append(name)
    
    for name, pkg in arm_packages.items():
        basename = pkg['basename']
        if basename not in arm_bases:
            arm_bases[basename] = {'version': pkg['version']}
    
    newer_in_x86 = []
    for basename, x86_data in x86_bases.items():
        # Check blacklist against basename (pkgbase) and individual package names
        blacklist_reason = None
        for pattern in blacklist:
            if fnmatch.fnmatch(basename, pattern):
                blacklist_reason = f"basename '{basename}' matches pattern '{pattern}'"
                break
            elif any(fnmatch.fnmatch(pkg_name, pattern) for pkg_name in x86_data['packages']):
                matching_pkg = next(pkg_name for pkg_name in x86_data['packages'] if fnmatch.fnmatch(pkg_name, pattern))
                blacklist_reason = f"package '{matching_pkg}' matches pattern '{pattern}'"
                break
        
        # Check if this is a missing package for blacklist tracking
        is_missing = basename not in arm_bases and basename not in arm_provides
        
        if blacklist_reason:
            if missing_packages_mode and is_missing:
                blacklisted_missing.append(basename)
            skipped_packages.append(f"{basename} ({blacklist_reason})")
            # Only add blacklisted packages to output if explicitly requested
            if force_packages and basename in force_packages:
                newer_in_x86.append({
                    'name': basename,
                    'force_latest': use_latest,
                    'use_aur': use_aur,
                    'skip': 1,
                    'depends': x86_data.get('depends', []),
                    'makedepends': x86_data.get('makedepends', []),
                    'provides': x86_data.get('provides', []),
                    **x86_data
                })
            continue
        
        # Skip bootstrap-only packages unless explicitly forced
        if basename in bootstrap_only and not force_packages:
            has_newer_version = (basename not in arm_bases or 
                               is_version_newer(arm_bases[basename]['version'], x86_data['version']))
            
            if has_newer_version:
                skipped_packages.append(f"{basename} (bootstrap-only package - newer version available, run bootstrap script)")
            continue
            
        should_include = False
        arm_version = "not found"
        
        if force_packages:
            # When --packages is specified, only include those packages
            should_include = basename in force_packages
            if basename in arm_bases:
                arm_version = arm_bases[basename]['version']
        elif missing_packages_mode:
            # Only include packages missing from aarch64
            should_include = basename not in arm_bases and basename not in arm_provides
        elif basename in arm_bases:
            # Compare basename versions using existing utility
            should_include = is_version_newer(arm_bases[basename]['version'], x86_data['version'])
            arm_version = arm_bases[basename]['version']
        else:
            # Package doesn't exist in aarch64 - don't auto-include all missing packages
            # Missing dependencies will be handled separately by find_missing_dependencies
            should_include = False
        
        if should_include:
            pkg_data = x86_data['pkg_data'].copy()
            pkg_data['name'] = basename
            
            # When using --packages, default to state repo version unless --use-latest is specified
            should_use_latest = False if force_packages else use_latest
            if force_packages and use_latest:
                should_use_latest = True
            
            newer_in_x86.append({
                'name': basename,
                'x86_version': x86_data['version'],
                'arm_version': arm_version,
                'force_latest': should_use_latest,
                'use_aur': bool(basename in aur_packages),
                **pkg_data
            })
    
    # Find and add missing dependencies only for missing packages (not for updates)
    if not missing_packages_mode:
        # Only check for missing dependencies of packages that don't exist in ARM at all
        missing_packages_only = []
        for pkg in newer_in_x86:
            basename = pkg['name']
            if basename not in arm_bases and basename not in arm_provides:
                missing_packages_only.append(pkg)
        
        if missing_packages_only:
            missing_deps = find_missing_dependencies(missing_packages_only, full_x86_packages, arm_packages)
            for dep_name in missing_deps:
                if dep_name in full_x86_packages:
                    pkg = full_x86_packages[dep_name]
                    basename = pkg['basename']
                    
                    # Check if basename is blacklisted
                    is_blacklisted = False
                    for pattern in blacklist:
                        if fnmatch.fnmatch(basename, pattern):
                            is_blacklisted = True
                            break
                    
                    if not is_blacklisted and basename not in [p['name'] for p in newer_in_x86]:
                        newer_in_x86.append({
                            'name': basename,
                            'force_latest': use_latest,
                            'use_aur': False,
                            **pkg
                        })
    
    return newer_in_x86, skipped_packages, blacklisted_missing

def preserve_package_order(packages, ordered_names):
    """
    Preserve exact order specified in command line for packages.
    
    Args:
        packages: List of package dictionaries
        ordered_names: List of package names in desired order
        
    Returns:
        list: Packages sorted in specified order with build_stage assigned sequentially
    """
    # Create lookup map
    pkg_map = {pkg['name']: pkg for pkg in packages}
    
    # Sort packages in specified order
    ordered_packages = []
    for i, name in enumerate(ordered_names):
        if name in pkg_map:
            pkg = pkg_map[name].copy()
            pkg['build_stage'] = i
            ordered_packages.append(pkg)
    
    # Add any remaining packages not in the ordered list
    remaining_names = set(pkg_map.keys()) - set(ordered_names)
    for i, name in enumerate(sorted(remaining_names)):
        pkg = pkg_map[name].copy()
        pkg['build_stage'] = len(ordered_names) + i
        ordered_packages.append(pkg)
    
    return ordered_packages

def sort_by_build_order(packages):
    """
    Sort packages by dependency order using topological sort.
    
    Ensures that dependencies are built before packages that depend on them.
    Handles circular dependencies gracefully and assigns build stages.
    
    Args:
        packages: List of package dictionaries with dependency information
        
    Returns:
        list: Packages sorted by build order with build_stage assigned
    """
    # Create maps for quick lookup
    pkg_map = {pkg['name']: pkg for pkg in packages}
    provides_map = {}
    
    # Build provides mapping - don't overwrite existing entries
    for pkg in packages:
        if pkg['name'] not in provides_map:
            provides_map[pkg['name']] = pkg
        for provide in pkg['provides']:
            provide_name = provide.split('=')[0]
            if provide_name not in provides_map:
                provides_map[provide_name] = pkg
    
    # Build dependency graph
    deps = {}
    for pkg in packages:
        deps[pkg['name']] = set()
        all_deps = pkg['depends'] + pkg['makedepends']
        if 'checkdepends' in pkg:
            all_deps += pkg['checkdepends']
        
        for dep_string in all_deps:
            # Split space-separated dependencies
            for dep in dep_string.split():
                dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                if dep_name in provides_map and provides_map[dep_name]['name'] in pkg_map:
                    deps[pkg['name']].add(provides_map[dep_name]['name'])
    
    # Calculate build stages
    stages = {}
    visiting = set()
    
    def get_stage(pkg_name):
        if pkg_name in stages:
            return stages[pkg_name]
        if pkg_name in visiting:
            return 0  # Break cycle
        
        visiting.add(pkg_name)
        max_dep_stage = -1
        for dep in deps.get(pkg_name, []):
            if dep in pkg_map:
                max_dep_stage = max(max_dep_stage, get_stage(dep))
        
        visiting.remove(pkg_name)
        stages[pkg_name] = max_dep_stage + 1
        return stages[pkg_name]
    
    # Assign stages to all packages
    for pkg in packages:
        get_stage(pkg['name'])
    
    # Add build_stage to packages and sort
    sorted_pkgs = []
    for pkg in packages:
        pkg_copy = pkg.copy()
        pkg_copy['build_stage'] = stages[pkg['name']]
        sorted_pkgs.append(pkg_copy)
    
    return sorted(sorted_pkgs, key=lambda x: x['build_stage'])

if __name__ == "__main__":
    # Clean up state from previous builds
    try:
        Path("last_successful.txt").unlink()
    except FileNotFoundError:
        pass
    
    parser = argparse.ArgumentParser(description='Generate build list by comparing Arch Linux package versions between x86_64 and AArch64 repositories')
    parser.add_argument('--arm-urls', nargs='+',
                        default=["https://arch-linux-repo.drzee.net/arch/core/os/aarch64/core.db",
                                 "https://arch-linux-repo.drzee.net/arch/extra/os/aarch64/extra.db"],
                        help='URLs for AArch64 repository databases')
    parser.add_argument('--aur', nargs='+',
                        help='Get specified packages from AUR (implies --packages mode)')
    parser.add_argument('--local', action='store_true',
                        help='Build packages from local PKGBUILDs only (use with --packages)')
    parser.add_argument('--packages', nargs='+',
                        help='Force rebuild specific packages by name')
    parser.add_argument('--preserve-order', action='store_true',
                        help='Preserve exact order specified in --packages (skip dependency sorting)')
    parser.add_argument('--blacklist', default='blacklist.txt',
                        help='File containing packages to skip (default: blacklist.txt)')
    parser.add_argument('--missing-packages', action='store_true',
                        help='Generate list of all packages present in x86_64 but missing from aarch64')
    parser.add_argument('--rebuild-repo', choices=['core', 'extra'],
                        help='Rebuild all packages from specified repository regardless of version')
    
    # Mutually exclusive group for git update options
    git_group = parser.add_mutually_exclusive_group()
    git_group.add_argument('--no-update', action='store_true',
                       help='Skip git updates, use existing PKGBUILDs')
    git_group.add_argument('--use-latest', action='store_true',
                        help='Use latest git commit of package source instead of version tag when building')
    
    args = parser.parse_args()
    
    # Validate --preserve-order requires --packages
    if args.preserve_order and not args.packages:
        print("ERROR: --preserve-order requires --packages to specify package order")
        sys.exit(1)
    
    # Validate package names if specified
    if args.packages:
        for pkg_name in args.packages:
            if not validate_package_name(pkg_name):
                print(f"ERROR: Invalid package name: {pkg_name}")
                sys.exit(1)
    
    # Handle --local implying --packages mode
    if args.local:
        if not args.packages:
            print("ERROR: --local requires --packages to specify which packages to build")
            sys.exit(1)
        
        # For local mode, create packages directly from local PKGBUILDs
        newer_packages = []
        for pkg_name in args.packages:
            pkg_dir = Path("pkgbuilds") / pkg_name
            if not pkg_dir.exists() or not (pkg_dir / "PKGBUILD").exists():
                print(f"ERROR: Local PKGBUILD not found for {pkg_name} at {pkg_dir}/PKGBUILD")
                sys.exit(1)
            
            newer_packages.append({
                'name': pkg_name,
                'basename': pkg_name,
                'version': 'local',
                'repo': 'extra',
                'depends': [],
                'makedepends': [],
                'provides': [],
                'force_latest': False,
                'use_aur': False,
                'use_local': True,
                'build_stage': 0
            })
        
        # Parse PKGBUILDs for dependencies
        newer_packages = fetch_pkgbuild_deps(newer_packages, True)
        if args.preserve_order:
            newer_packages = preserve_package_order(newer_packages, args.packages)
        else:
            newer_packages = sort_by_build_order(newer_packages)
        write_results(newer_packages, args)
        sys.exit(0)
    
    # Handle --aur implying --packages mode
    if args.aur:
        if args.packages:
            # Merge AUR packages with regular packages
            args.packages.extend(args.aur)
        else:
            args.packages = args.aur
        # Set AUR flag for the specified packages
        args.use_aur_for_packages = set(args.aur)
        args.aur = True
        
        # For pure AUR mode, skip all comparison logic
        if not args.packages or set(args.packages) == args.use_aur_for_packages:
            # Create packages directly from AUR list
            newer_packages = []
            for pkg_name in args.use_aur_for_packages:
                newer_packages.append({
                    'name': pkg_name,
                    'basename': pkg_name,
                    'version': 'AUR',
                    'repo': 'aur',
                    'depends': [],
                    'makedepends': [],
                    'provides': [],
                    'force_latest': not args.no_update,  # Update to latest unless --no-update
                    'use_aur': True,
                    'use_local': False,
                    'build_stage': 0
                })
            
            # Fetch PKGBUILDs and write results
            if newer_packages:
                print("Processing PKGBUILDs for dependency information...")
                newer_packages = fetch_pkgbuild_deps(newer_packages, args.no_update)
            
            if args.preserve_order:
                newer_packages = preserve_package_order(newer_packages, list(args.use_aur_for_packages))
            else:
                newer_packages = sort_by_build_order(newer_packages)
            write_results(newer_packages, args)
            sys.exit(0)
    else:
        args.use_aur_for_packages = set()
        args.aur = False
    
    x86_packages = {}
    if args.packages and not args.aur:
        print(f"Looking up versions for {len(args.packages)} specified packages...")
        # Load full x86_64 packages for dependency resolution
        all_x86_packages = load_x86_64_packages(download=not args.no_update)
        
        # Filter to only requested packages for comparison, but keep full list for dependency resolution
        filtered_x86_packages = {}
        for pkg_name in args.packages:
            if pkg_name in all_x86_packages:
                filtered_x86_packages[pkg_name] = all_x86_packages[pkg_name]
            else:
                # Check if it's a basename (pkgbase) instead of package name
                found_by_basename = False
                for name, pkg_data in all_x86_packages.items():
                    if pkg_data['basename'] == pkg_name:
                        filtered_x86_packages[name] = pkg_data
                        found_by_basename = True
                        break
                
                if not found_by_basename:
                    # Check if package exists but is ARCH=any (filtered out)
                    arch_any_found = False
                    is_arch_any = False
                    for repo in ['core', 'extra']:
                        try:
                            db_filename = f"{repo}_x86_64.db"
                            if os.path.exists(db_filename):
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
                                            
                                            if ('NAME' in data and data['NAME'][0] == pkg_name) or \
                                               ('BASE' in data and data['BASE'][0] == pkg_name):
                                                if data.get('ARCH', [''])[0] == 'any':
                                                    print(f"WARNING: Package {pkg_name} is ARCH=any and doesn't need rebuilding for AArch64")
                                                    print(f"         ARCH=any packages work on all architectures without modification")
                                                    is_arch_any = True
                                                arch_any_found = True
                                                break
                                    if arch_any_found:
                                        break
                        except Exception:
                            continue
                    
                    if not arch_any_found:
                        print(f"ERROR: Package {pkg_name} not found in x86_64 repositories")
                        sys.exit(1)
                    elif is_arch_any:
                        continue  # Skip ARCH=any packages
        x86_packages = filtered_x86_packages
        # Store full package list for dependency resolution
        full_x86_packages = all_x86_packages
    elif not args.aur:
        # Load x86_64 packages using shared function
        repos = [args.rebuild_repo] if args.rebuild_repo else None
        x86_packages = load_x86_64_packages(download=not args.no_update, repos=repos)
        print(f"Loaded {len(x86_packages)} x86_64 packages")
        # Use same list for dependency resolution
        full_x86_packages = x86_packages
    else:
        print("Skipping x86_64 parsing (using AUR)")
        full_x86_packages = {}
        if args.packages:
            for pkg_name in args.packages:
                x86_packages[pkg_name] = {
                    'name': pkg_name,
                    'basename': pkg_name,
                    'version': 'latest',
                    'depends': [],
                    'makedepends': [],
                    'provides': [],
                    'repo': 'extra'
                }
    
    # Load AArch64 packages using shared function
    arm_packages = load_aarch64_packages(download=not args.no_update, urls=args.arm_urls)
    print(f"Loaded {len(arm_packages)} AArch64 packages")
    
    print("Comparing versions...")
    # Skip blacklist entirely when using --packages
    if args.packages:
        blacklist = []
        print("Ignoring blacklist (using --packages)")
    else:
        blacklist = load_blacklist(args.blacklist)
        if blacklist:
            print(f"Loaded blacklist with {len(blacklist)} packages")
    
    newer_packages, skipped_packages, blacklisted_missing = compare_versions(x86_packages, arm_packages, args.packages, blacklist, args.missing_packages, args.use_aur_for_packages, args.use_latest)
    
    # Report blacklisted missing packages
    if args.missing_packages and blacklisted_missing:
        print(f"Found {len(newer_packages)} missing packages, {len(blacklisted_missing)} blacklisted: {' '.join(blacklisted_missing)}")
    elif args.missing_packages:
        print(f"Found {len(newer_packages)} missing packages")
    
    # Handle --rebuild-repo option
    if args.rebuild_repo:
        print(f"Rebuilding all packages from {args.rebuild_repo} repository...")
        repo_packages = []
        # Exclude core toolchain packages that are handled by bootstrap_toolchain.py
        toolchain_packages = {'gcc', 'binutils', 'glibc'}
        
        for pkg_name, pkg_data in x86_packages.items():
            if pkg_data['repo'] == args.rebuild_repo:
                # Skip core toolchain packages
                if pkg_data['basename'] in toolchain_packages:
                    continue
                    
                # Skip blacklisted packages
                blacklist_reason = None
                if blacklist:
                    for pattern in blacklist:
                        if fnmatch.fnmatch(pkg_data['basename'], pattern):
                            blacklist_reason = f"basename '{pkg_data['basename']}' matches pattern '{pattern}'"
                            break
                        if fnmatch.fnmatch(pkg_name, pattern):
                            blacklist_reason = f"package '{pkg_name}' matches pattern '{pattern}'"
                            break
                
                if not blacklist_reason:
                    repo_packages.append({
                        'name': pkg_name,
                        'basename': pkg_data['basename'],
                        'version': pkg_data['version'],
                        'repo': pkg_data['repo'],
                        'depends': pkg_data['depends'],
                        'makedepends': pkg_data['makedepends'],
                        'provides': pkg_data['provides'],
                        'force_latest': args.use_latest,
                        'use_aur': False
                    })
        newer_packages = repo_packages
        print(f"Found {len(newer_packages)} packages in {args.rebuild_repo} repository")
    
    # Fetch PKGBUILDs for packages that need building to get complete dependency info (always enabled)
    if newer_packages:
        print("Processing PKGBUILDs for dependency information...")
        newer_packages = fetch_pkgbuild_deps(newer_packages, args.no_update)
    
    # Check if any skipped packages are dependencies of packages being built
    if skipped_packages and newer_packages:
        # Extract package names from skipped packages (remove reason text)
        skipped_names = set()
        for skipped in skipped_packages:
            # Extract package name before the first space and parenthesis
            name = skipped.split(' (')[0]
            skipped_names.add(name)
        
        # Check if any skipped packages are dependencies
        relevant_skipped = []
        for pkg in newer_packages:
            all_deps = pkg.get('depends', []) + pkg.get('makedepends', []) + pkg.get('checkdepends', [])
            for dep in all_deps:
                # Remove version constraints from dependency names
                dep_name = dep.split('>=')[0].split('<=')[0].split('=')[0].split('<')[0].split('>')[0]
                if dep_name in skipped_names:
                    # Find the original skipped entry
                    for skipped in skipped_packages:
                        if skipped.startswith(dep_name + ' ('):
                            relevant_skipped.append(skipped)
                            break
        
        if relevant_skipped:
            # Extract just the package names (remove the reason text)
            unique_skipped_names = sorted(set(skipped.split(' (')[0] for skipped in relevant_skipped))
            print(f"Skipped {len(unique_skipped_names)} blacklisted packages that are dependencies: {', '.join(unique_skipped_names)}")
    elif skipped_packages and not newer_packages:
        # If no packages to build but some were skipped, show count only
        print(f"Skipped {len(skipped_packages)} blacklisted packages")
    
    if args.packages:
        # Separate packages that need updates vs rebuilds
        rebuild_packages = []
        update_packages = []
        for pkg_name in args.packages:
            if pkg_name in newer_packages:
                pkg = newer_packages[pkg_name]
                if pkg.get('x86_version') == pkg.get('arm_version'):
                    rebuild_packages.append(pkg_name)
                else:
                    update_packages.append(pkg_name)
        
        rebuild_list = ', '.join(f"\033[1m{pkg}\033[0m" for pkg in rebuild_packages) if rebuild_packages else ""
        if rebuild_list:
            print(f"Found {len(newer_packages)} packages (including rebuilds: {rebuild_list})")
        else:
            print(f"Found {len(newer_packages)} packages")
    else:
        print(f"Found {len(newer_packages)} packages where x86_64 is newer")
    
    if args.preserve_order and args.packages:
        print("Preserving command line order...")
        sorted_packages = preserve_package_order(newer_packages, args.packages)
    else:
        print("Sorting by build order...")
        sorted_packages = sort_by_build_order(newer_packages)
    
    write_results(sorted_packages, args)
    
    # Build statistics
    if sorted_packages:
        # Calculate statistics excluding blacklisted packages
        buildable_packages = [pkg for pkg in sorted_packages if pkg.get('skip', 0) != 1]
        stage_counts = {}
        for pkg in buildable_packages:
            stage = pkg['build_stage']
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
        
        if stage_counts:
            max_stage = max(stage_counts.keys())
            if len(sorted_packages) > len(buildable_packages):
                blacklisted_requested = len(sorted_packages) - len(buildable_packages)
                print(f"Skipped {blacklisted_requested} blacklisted packages marked for upgrade:")
                for pkg in sorted_packages:
                    if pkg.get('skip', 0) == 1:
                        print(f"  - {pkg['name']} ({pkg.get('blacklist_reason', 'blacklisted')})")
            if skipped_packages:
                print(f"Skipped {len(skipped_packages)} total blacklisted packages")
    
    print("Keeping downloaded database files...")
    
    # Check if any critical bootstrap toolchain packages need updates (skip for --rebuild-repo)
    if sorted_packages and not args.rebuild_repo:
        critical_toolchain_packages = ['linux-api-headers', 'gcc', 'binutils']
        outdated_toolchain = []
        
        for pkg in sorted_packages:
            if pkg['name'] in critical_toolchain_packages:
                outdated_toolchain.append(pkg['name'])
        
        if outdated_toolchain:
            print(f"\n{'='*60}")
            print(f"⚠️  WARNING: Bootstrap toolchain packages are outdated!")
            print(f"{'='*60}")
            print(f"The following toolchain packages need updates:")
            for pkg_name in sorted(set(outdated_toolchain)):
                print(f"  - {pkg_name}")
            print(f"\nConsider running a bootstrap build:")
            print(f"  ./build_packages.py --bootstrap-toolchain")
            print(f"{'='*60}")
    
    buildable_count = len([pkg for pkg in sorted_packages if pkg.get('skip', 0) != 1])
    print(f"Complete! Found {buildable_count} packages to update.")
