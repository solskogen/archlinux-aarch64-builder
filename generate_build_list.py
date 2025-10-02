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

import json
import os
import argparse
import fnmatch
import sys
import datetime
import subprocess
import re
import shutil
from pathlib import Path
from packaging import version
from utils import (
    load_blacklist, load_x86_64_packages, load_target_arch_packages,
    validate_package_name, safe_path_join, is_version_newer,
    PACKAGE_SKIP_FLAG, parse_pkgbuild_deps, parse_database_file, X86_64_MIRROR,
    PKGBUILDS_DIR, SEPARATOR_WIDTH, get_target_architecture,
    should_skip_package, compare_bin_package_versions, extract_packages, find_missing_dependencies,
    load_packages_with_any, config, load_all_packages_parallel
)

# ============================================================
# Helper Functions for Package Classification
# ============================================================

def is_bootstrap_package(pkg_name):
    """Check if package is bootstrap-only (excluded from normal builds)"""
    bootstrap_only = {'linux-api-headers', 'glibc', 'binutils', 'gcc'}
    return pkg_name in bootstrap_only

# ============================================================
# Package Version Comparison Logic  
# ============================================================



def get_provides_mapping():
    """
    Get basename to provides mapping from upstream x86_64 databases.
    
    Downloads and parses official Arch Linux databases to build a mapping
    of package names to what they provide. This is used for dependency
    resolution when packages depend on virtual packages.
    
    Returns:
        dict: Mapping of provided names to providing package basenames
    """
    import urllib.request
    import tarfile
    import tempfile
    
    mirror_url = X86_64_MIRROR
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
        cycles = {}
        unique_packages = set()
        
        for pkg in packages:
            stage = pkg.get('build_stage', 0)
            stages[stage] = stages.get(stage, 0) + 1
            unique_packages.add(pkg['name'])
            
            # Track cycle information
            if pkg.get('cycle_group') is not None:
                cycle_id = pkg['cycle_group']
                if cycle_id not in cycles:
                    cycles[cycle_id] = set()
                cycles[cycle_id].add(pkg['name'])
        
        print(f"Unique packages: {len(unique_packages)}")
        print(f"Total build stages: {max(stages.keys()) + 1 if stages else 0}")
        
        if cycles:
            print(f"Dependency cycles: {len(cycles)}")
            for cycle_id, cycle_pkgs in cycles.items():
                print(f"  Cycle {cycle_id + 1}: {', '.join(sorted(cycle_pkgs))} (built twice)")
        
        print("Packages per stage:")
        for stage in sorted(stages.keys()):
            print(f"  Stage {stage + 1}: {stages[stage]} packages")






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
    
    Path(PKGBUILDS_DIR).mkdir(exist_ok=True)
    
    # Filter out blacklisted packages for PKGBUILD fetching
    packages_to_fetch = [pkg for pkg in packages_to_build if pkg.get('skip', 0) != 1]
    blacklisted_packages = [pkg for pkg in packages_to_build if pkg.get('skip', 0) == 1]
    
    total = len(packages_to_fetch)
    for i, pkg in enumerate(packages_to_fetch, 1):
        name = pkg['name']
        basename = pkg.get('basename', name)  # Use basename for git operations
        pkgbuild_directory = Path(PKGBUILDS_DIR) / basename
        pkgbuild_path = pkgbuild_directory / "PKGBUILD"
        
        # Check if PKGBUILD exists and get current version
        current_version = None
        if pkgbuild_path.exists():
            try:
                with open(pkgbuild_path, 'r') as f:
                    content = f.read()
                    # Extract pkgver, pkgrel, and epoch - only if they don't contain complex variables
                    pkgver_match = re.search(r'^pkgver=(.+)$', content, re.MULTILINE)
                    pkgrel_match = re.search(r'^pkgrel=(.+)$', content, re.MULTILINE)
                    epoch_match = re.search(r'^epoch=(.+)$', content, re.MULTILINE)
                    
                    if pkgver_match and pkgrel_match:
                        pkgver = pkgver_match.group(1).strip('\'"')
                        pkgrel = pkgrel_match.group(1).strip('\'"')
                        epoch = epoch_match.group(1).strip('\'"') if epoch_match else None
                        
                        # Only use if no complex variable substitution
                        if not ('${' in pkgver or '$(' in pkgver):
                            if epoch:
                                current_version = f"{epoch}:{pkgver}-{pkgrel}"
                            else:
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
                                         cwd=PKGBUILDS_DIR, check=True, 
                                         capture_output=True, text=True)
                else:
                    print(f"[{i}/{total}] Processing {name} (fetching {target_version})...")
                    # Use git directly for more reliable tag switching
                    # Convert ++ to plusplus for GitLab URLs
                    gitlab_basename = basename.replace('++', 'plusplus')
                    if pkg.get('force_latest', False):
                        # Clone and stay on main branch
                        result = subprocess.run(["git", "clone", f"https://gitlab.archlinux.org/archlinux/packaging/packages/{gitlab_basename}.git", basename], 
                                             cwd=PKGBUILDS_DIR, check=True, 
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
                            subprocess.run(["git", "fetch", "origin"], cwd=f"pkgbuilds/{basename}", check=True, capture_output=True)
                            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=f"pkgbuilds/{basename}", check=True, capture_output=True)
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
                            # Reset to clean state before updating to latest
                            subprocess.run(["git", "reset", "--hard"], cwd=pkg_repo_dir, check=True, capture_output=True)
                            subprocess.run(["git", "fetch", "origin"], cwd=pkg_repo_dir, check=True, capture_output=True, text=True)
                            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=pkg_repo_dir, check=True, capture_output=True, text=True)
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








def compare_versions(x86_packages, target_packages, force_packages=None, blacklist=None, missing_packages_mode=False, aur_packages=None, use_latest=False):
    """
    Compare package versions between x86_64 and target architecture repositories.
    
    Identifies packages that need building by comparing versions and handling
    various scenarios like missing packages, blacklisted packages, and
    bootstrap-only packages.
    
    Args:
        x86_packages: Dictionary of x86_64 packages
        target_packages: Dictionary of target architecture packages  
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
    # Note: Using helper function for consistency
    
    skipped_packages = []
    blacklisted_missing = []
    
    # Group packages by basename
    x86_bases = {}
    target_bases = {}
    
    # ============================================================
    # Group packages by basename and build provides mapping
    # ============================================================
    
    # Build provides mapping for target architecture packages (only if needed for missing deps)
    target_provides = {name: pkg for name, pkg in target_packages.items()}
    if not missing_packages_mode:
        for pkg in target_packages.values():
            for provide in pkg['provides']:
                provide_name = provide.split('=')[0]
                target_provides[provide_name] = pkg
    
    # Group x86_64 packages by basename
    for name, pkg in x86_packages.items():
        basename = pkg['basename']
        if basename not in x86_bases:
            x86_bases[basename] = {'packages': [], 'version': pkg['version'], 'pkg_data': pkg}
        x86_bases[basename]['packages'].append(name)
    
    for name, pkg in target_packages.items():
        basename = pkg['basename']
        if basename not in target_bases:
            target_bases[basename] = {'version': pkg['version']}
    
    newer_in_x86 = []
    bin_package_warnings = []
    
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
        is_missing = basename not in target_bases and basename not in target_provides
        
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
        if is_bootstrap_package(basename) and not force_packages:
            has_newer_version = (basename not in target_bases or 
                               is_version_newer(target_bases[basename]['version'], x86_data['version']))
            
            if has_newer_version:
                skipped_packages.append(f"{basename} (bootstrap-only package - newer version available, run bootstrap script)")
            continue
            
        should_include = False
        target_version = "not found"
        
        if force_packages:
            # When --packages is specified, check if any individual package name is requested
            should_include = (basename in force_packages or 
                            any(pkg_name in force_packages for pkg_name in x86_data['packages']))
            if basename in target_bases:
                target_version = target_bases[basename]['version']
            elif basename in arm_provides and arm_provides[basename]['name'].endswith('-bin'):
                # Check if a -bin package provides this package
                bin_pkg = arm_provides[basename]
                provided_version = None
                for provide in bin_pkg['provides']:
                    if provide.startswith(f"{basename}="):
                        provided_version = provide.split('=')[1]
                        break
                
                if provided_version:
                    comparison = compare_bin_package_versions(provided_version, x86_data['version'])
                    if comparison == -1:
                        bin_package_warnings.append(f"WARNING: {bin_pkg['name']} (provides {basename}={provided_version}) is outdated compared to x86_64 {basename} ({x86_data['version']})")
                    elif comparison == 1:
                        bin_package_warnings.append(f"INFO: {bin_pkg['name']} (provides {basename}={provided_version}) is newer than x86_64 {basename} ({x86_data['version']})")
                should_include = False  # Don't build -bin packages
                target_version = f"provided by {bin_pkg['name']}"
        elif missing_packages_mode:
            # Only include packages missing from aarch64
            should_include = basename not in target_bases and basename not in target_provides
        elif basename in target_bases:
            # Compare basename versions using existing utility
            should_include = is_version_newer(target_bases[basename]['version'], x86_data['version'])
            target_version = target_bases[basename]['version']
        else:
            # Check if a -bin package provides this package
            if basename in target_provides and target_provides[basename]['name'].endswith('-bin'):
                bin_pkg = target_provides[basename]
                # Extract version from provides (e.g., "electron37=37.3.1" -> "37.3.1")
                provided_version = None
                for provide in bin_pkg['provides']:
                    if provide.startswith(f"{basename}="):
                        provided_version = provide.split('=')[1]
                        break
                
                if provided_version:
                    comparison = compare_bin_package_versions(provided_version, x86_data['version'])
                    if comparison == -1:
                        bin_package_warnings.append(f"WARNING: {bin_pkg['name']} (provides {basename}={provided_version}) is outdated compared to x86_64 {basename} ({x86_data['version']})")
                    elif comparison == 1:
                        bin_package_warnings.append(f"INFO: {bin_pkg['name']} (provides {basename}={provided_version}) is newer than x86_64 {basename} ({x86_data['version']})")
                should_include = False
                target_version = f"provided by {bin_pkg['name']}"
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
                'target_version': target_version,
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
            if basename not in target_bases and basename not in target_provides:
                missing_packages_only.append(pkg)
        
        if missing_packages_only:
            missing_deps = find_missing_dependencies(missing_packages_only, full_x86_packages, target_packages)
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
    
    return newer_in_x86, skipped_packages, blacklisted_missing, bin_package_warnings

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

def detect_dependency_cycles(packages):
    """
    Detect dependency cycles in the package list.
    
    Returns:
        dict: Maps package names to their cycle group ID (if in a cycle)
    """
    # Create maps for quick lookup
    pkg_map = {pkg['name']: pkg for pkg in packages}
    provides_map = {}
    
    # Build provides mapping
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
            for dep in dep_string.split():
                dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                if dep_name in provides_map and provides_map[dep_name]['name'] in pkg_map:
                    deps[pkg['name']].add(provides_map[dep_name]['name'])
    
    # Find strongly connected components (cycles)
    visited = set()
    rec_stack = set()
    cycles = {}
    cycle_id = 0
    
    def find_cycles(node, path):
        nonlocal cycle_id
        if node in rec_stack:
            # Found a cycle - mark all nodes in the cycle
            cycle_start = path.index(node)
            cycle_nodes = path[cycle_start:]
            if len(cycle_nodes) > 1:  # Only real cycles (more than 1 node)
                for cycle_node in cycle_nodes:
                    cycles[cycle_node] = cycle_id
                cycle_id += 1
            return
        
        if node in visited:
            return
        
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        
        for neighbor in deps.get(node, []):
            if neighbor in pkg_map:
                find_cycles(neighbor, path)
        
        path.pop()
        rec_stack.remove(node)
    
    # Find all cycles
    for pkg_name in pkg_map:
        if pkg_name not in visited:
            find_cycles(pkg_name, [])
    
    return cycles

def sort_by_build_order(packages):
    """
    Sort packages by dependency order using topological sort.
    
    Detects dependency cycles and marks packages for double-build when
    all packages in a cycle need upgrading.
    
    Args:
        packages: List of package dictionaries with dependency information
        
    Returns:
        list: Packages sorted by build order with build_stage and cycle info assigned
    """
    # Detect dependency cycles
    cycles = detect_dependency_cycles(packages)
    
    # Group packages by cycle
    cycle_groups = {}
    for pkg_name, cycle_id in cycles.items():
        if cycle_id not in cycle_groups:
            cycle_groups[cycle_id] = []
        cycle_groups[cycle_id].append(pkg_name)
    
    # Report detected cycles
    if cycle_groups:
        print(f"\nDetected {len(cycle_groups)} dependency cycle(s):")
        for cycle_id, cycle_pkgs in cycle_groups.items():
            print(f"  Cycle {cycle_id + 1}: {' ↔ '.join(cycle_pkgs)}")
            print(f"    These packages will be built twice to ensure compatibility")
    
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
    
    # Create final package list with cycle information
    result_packages = []
    next_stage = 0
    
    # Add non-cycle packages first, tracking stages
    stage_map = {}
    for pkg in packages:
        if pkg['name'] not in cycles:
            pkg_copy = pkg.copy()
            pkg_copy['build_stage'] = stages[pkg['name']]
            pkg_copy['cycle_group'] = None
            pkg_copy['cycle_stage'] = None
            result_packages.append(pkg_copy)
            next_stage = max(next_stage, stages[pkg['name']] + 1)
    
    # Add packages in cycles twice (consecutive stages)
    for cycle_id, cycle_pkgs in cycle_groups.items():
        # Stage 1: Build all packages in cycle
        for pkg_name in cycle_pkgs:
            pkg = pkg_map[pkg_name]
            pkg_copy = pkg.copy()
            pkg_copy['build_stage'] = next_stage
            pkg_copy['cycle_group'] = cycle_id
            pkg_copy['cycle_stage'] = 1
            result_packages.append(pkg_copy)
        
        # Stage 2: Build all packages in cycle again
        for pkg_name in cycle_pkgs:
            pkg = pkg_map[pkg_name]
            pkg_copy = pkg.copy()
            pkg_copy['build_stage'] = next_stage + 1
            pkg_copy['cycle_group'] = cycle_id
            pkg_copy['cycle_stage'] = 2
            result_packages.append(pkg_copy)
        
        next_stage += 2  # Move to next available stage after this cycle
    
    return sorted(result_packages, key=lambda x: (x['build_stage'], x.get('cycle_stage', 0)))

if __name__ == "__main__":
    # Clean up state from previous builds
    try:
        Path("last_successful.txt").unlink()
    except FileNotFoundError:
        pass
    
    target_arch = get_target_architecture()
    
    parser = argparse.ArgumentParser(description=f'Generate build list by comparing Arch Linux package versions between x86_64 and {target_arch} repositories')
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
                    'repo': 'extra',
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
        # Load packages using shared parallel function
        all_x86_packages, target_packages = load_all_packages_parallel(download=not args.no_update)
        
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
        # Load packages using shared parallel function
        print("Loading packages...")
        repo_filter = [args.rebuild_repo] if args.rebuild_repo else None
        x86_packages, target_packages = load_all_packages_parallel(download=not args.no_update, x86_repos=repo_filter, target_repos=repo_filter)
        
        # Use same list for dependency resolution
        full_x86_packages = x86_packages
    else:
        print("Skipping x86_64 parsing (using AUR)")
        full_x86_packages = {}
        # For AUR mode, we still need target packages for comparison
        target_arch = get_target_architecture()
        target_urls = [
            config.get('build', 'target_core_url', fallback=f"https://arch-linux-repo.drzee.net/arch/core/os/{target_arch}/core.db"),
            config.get('build', 'target_extra_url', fallback=f"https://arch-linux-repo.drzee.net/arch/extra/os/{target_arch}/extra.db")
        ]
        target_packages = load_packages_with_any(target_urls, f'_{target_arch}', download=not args.no_update)
        print(f"Loaded {len(target_packages)} {target_arch} packages")
        
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
    
    print("Comparing versions...")
    # Skip blacklist entirely when using --packages
    if args.packages:
        blacklist = []
        print("Ignoring blacklist (using --packages)")
    else:
        blacklist = load_blacklist(args.blacklist)
        if blacklist:
            print(f"Loaded blacklist with {len(blacklist)} packages")
    
    newer_packages, skipped_packages, blacklisted_missing, bin_package_warnings = compare_versions(x86_packages, target_packages, args.packages, blacklist, args.missing_packages, args.use_aur_for_packages, args.use_latest)
    
    # Report blacklisted missing packages
    if args.missing_packages and blacklisted_missing:
        print(f"Found {len(newer_packages)} missing packages, {len(blacklisted_missing)} blacklisted: {' '.join(blacklisted_missing)}")
    elif args.missing_packages:
        print(f"Found {len(newer_packages)} missing packages")
    
    # Report -bin package warnings
    if bin_package_warnings:
        print("\n" + "="*SEPARATOR_WIDTH)
        for warning in bin_package_warnings:
            print(warning)
        print("="*SEPARATOR_WIDTH)
    
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
                if pkg.get('x86_version') == pkg.get('target_version'):
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
