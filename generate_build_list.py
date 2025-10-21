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
import configparser
import subprocess
import re
import shutil
import tarfile
from pathlib import Path
from packaging import version
from utils import (
    load_blacklist, load_x86_64_packages, load_target_arch_packages,
    validate_package_name, safe_path_join, is_version_newer,
    PACKAGE_SKIP_FLAG, parse_pkgbuild_deps, parse_database_file, X86_64_MIRROR,
    PKGBUILDS_DIR, SEPARATOR_WIDTH, get_target_architecture,
    compare_bin_package_versions, find_missing_dependencies,
    load_packages_with_any, config, load_packages_unified
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



def write_results(packages, args):
    """Write results to packages_to_build.json"""
    print("Writing results to packages_to_build.json...")
    
    # Prepare packages for JSON output - use build dependencies instead of filtered ones
    json_packages = []
    for pkg in packages:
        json_pkg = pkg.copy()
        # Replace filtered dependencies with complete build dependencies
        if 'build_depends' in pkg:
            json_pkg['depends'] = pkg['build_depends']
        if 'build_makedepends' in pkg:
            json_pkg['makedepends'] = pkg['build_makedepends']
        if 'build_checkdepends' in pkg:
            json_pkg['checkdepends'] = pkg['build_checkdepends']
        
        # Remove internal fields
        for key in ['build_depends', 'build_makedepends', 'build_checkdepends']:
            json_pkg.pop(key, None)
        
        json_packages.append(json_pkg)
    
    output_data = {
        "_command": " ".join(sys.argv),
        "_timestamp": datetime.datetime.now().isoformat(),
        "packages": json_packages
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
    from utils import safe_command_execution
    
    if not safe_command_execution(["pkgctl", "--version"], "pkgctl version check", 
                                 capture_output=True, exit_on_error=False):
        print("Error: pkgctl not found. Please install devtools package.")
        sys.exit(1)
    
    if not safe_command_execution(["git", "--version"], "git version check", 
                                 capture_output=True, exit_on_error=False):
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
                import shlex
                temp_script = f"""#!/bin/bash
cd {shlex.quote(str(pkgbuild_path.parent))}
source PKGBUILD 2>/dev/null || exit 1
fullver="$pkgver-$pkgrel"
if [[ -n $epoch ]]; then
    fullver="$epoch:$fullver"
fi
echo "$fullver"
"""
                result = subprocess.run(['bash', '-c', temp_script], 
                                      capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    current_version = result.stdout.strip()
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
                    # Clone repository (convert ++ to plusplus for GitLab URLs)
                    result = subprocess.run(["git", "clone", f"https://gitlab.archlinux.org/archlinux/packaging/packages/{basename.replace('++', 'plusplus')}.git", basename], 
                                         cwd=PKGBUILDS_DIR, check=True, 
                                         capture_output=True, text=True)
                    
                    if not pkg.get('force_latest', False):
                        # Checkout specific version tag
                        version_tag = pkg['version']
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
                    # Ensure we're on main branch before pulling
                    subprocess.run(["git", "checkout", "main"], cwd=pkg_repo_dir, check=True, capture_output=True)
                    subprocess.run(["git", "pull"], cwd=pkg_repo_dir, check=True, capture_output=True)
                    
                    # Re-read version after git pull for --use-latest
                    try:
                        import shlex
                        temp_script = f"""#!/bin/bash
cd {shlex.quote(str(pkgbuild_path.parent))}
source PKGBUILD 2>/dev/null || exit 1
fullver="$pkgver-$pkgrel"
if [[ -n $epoch ]]; then
    fullver="$epoch:$fullver"
fi
echo "$fullver"
"""
                        result = subprocess.run(['bash', '-c', temp_script], 
                                              capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            pkg['version'] = result.stdout.strip()
                    except Exception:
                        pass  # Keep original version if parsing fails
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
                print(f"ERROR: Failed to update {basename}: {e}")
                if hasattr(e, 'stderr') and e.stderr:
                    print(f"Git error: {e.stderr.strip()}")
                print(f"Please resolve git conflicts in pkgbuilds/{basename} and run again.")
                sys.exit(1)
        else:
            # Repository exists and is up to date
            print(f"[{i}/{total}] Processing {name} (PKGBUILD already up to date: {current_version or target_version})...")
        
        # Parse dependencies from existing PKGBUILD (skip provides - use database)
        if pkgbuild_path.exists():
            deps = parse_pkgbuild_deps(pkgbuild_path)
            # Keep original provides from database, only update dependencies
            original_provides = pkg.get('provides', [])
            pkg.update(deps)
            pkg['provides'] = original_provides
    
    # Return combined list with blacklisted packages (unchanged) and fetched packages (with updated deps)
    all_packages = packages_to_fetch + blacklisted_packages
    
    # Create build list for dependency ordering (filtered dependencies)
    build_list_names = {pkg['name'] for pkg in all_packages}
    build_list_basenames = {pkg.get('basename', pkg['name']) for pkg in all_packages}
    
    # Build provides mapping from packages in the build list
    build_list_provides = {}
    for pkg in all_packages:
        pkg_name = pkg['name']
        basename = pkg.get('basename', pkg_name)
        # Package provides itself
        build_list_provides[pkg_name] = basename
        build_list_provides[basename] = basename
        # Add explicit provides
        for provide in pkg.get('provides', []):
            provide_name = provide.split('=')[0]
            build_list_provides[provide_name] = basename
    
    # Add common split package patterns for packages in build list
    # This handles cases like linux providing linux-headers, linux-docs, etc.
    basenames_in_build = {pkg.get('basename', pkg['name']) for pkg in all_packages}
    for basename in basenames_in_build:
        # Common split package suffixes
        for suffix in ['-headers', '-docs', '-devel', '-dev']:
            split_name = basename + suffix
            if split_name not in build_list_provides:
                build_list_provides[split_name] = basename
    
    for pkg in all_packages:
        # Keep original dependencies for build system
        pkg['build_depends'] = pkg.get('depends', []).copy()
        pkg['build_makedepends'] = pkg.get('makedepends', []).copy() 
        pkg['build_checkdepends'] = pkg.get('checkdepends', []).copy()
        
        # Filter dependencies for build ordering only
        for dep_type in ['depends', 'makedepends', 'checkdepends']:
            if dep_type in pkg:
                filtered_deps = []
                for dep in pkg[dep_type]:
                    dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                    if (dep_name in build_list_names or 
                        dep_name in build_list_basenames or
                        dep_name in build_list_provides):
                        filtered_deps.append(dep)
                pkg[dep_type] = filtered_deps
    
    
    return all_packages








def compare_versions(x86_packages, target_packages, force_packages=None, blacklist=None, missing_packages_mode=False, aur_packages=None, use_latest=False, full_x86_packages=None):
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
            
        # For missing packages mode, skip all version comparison logic
        if missing_packages_mode:
            should_include = basename not in target_bases and basename not in target_provides
            target_version = "not found"
            
            # Check if package depends on blacklisted packages
            if should_include:
                pkg_data = x86_data['pkg_data']
                all_deps = pkg_data.get('depends', []) + pkg_data.get('makedepends', [])
                
                for dep in all_deps:
                    # Strip version constraints from dependency name
                    dep_name = dep.split('=')[0].split('<')[0].split('>')[0].split('>=')[0].split('<=')[0]
                    
                    # Check if dependency matches any blacklist pattern
                    for pattern in blacklist:
                        if fnmatch.fnmatch(dep_name, pattern):
                            should_include = False
                            skipped_packages.append(f"{basename} (depends on blacklisted package: {dep_name})")
                            break
                    
                    if not should_include:
                        break
        elif force_packages:
            # When --packages is specified, check if any individual package name is requested
            should_include = (basename in force_packages or 
                            any(pkg_name in force_packages for pkg_name in x86_data['packages']))
            if basename in target_bases:
                target_version = target_bases[basename]['version']
            elif basename in target_provides and target_provides[basename]['name'].endswith('-bin'):
                # Check if a -bin package provides this package
                bin_pkg = target_provides[basename]
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
                # For --packages mode, still include the package even if -bin exists
                target_version = f"provided by {bin_pkg['name']}"
            else:
                target_version = "not found"
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
                # Package doesn't exist in target architecture - don't auto-include all missing packages
                # Missing dependencies will be handled separately by find_missing_dependencies
                should_include = False
        
        if should_include:
            # Double-check blacklist before adding to results (safety check)
            is_blacklisted = False
            for pattern in blacklist:
                if fnmatch.fnmatch(basename, pattern) or any(fnmatch.fnmatch(pkg_name, pattern) for pkg_name in x86_data['packages']):
                    is_blacklisted = True
                    break
            
            if is_blacklisted:
                continue  # Skip blacklisted packages
                
            pkg_data = x86_data['pkg_data'].copy()
            pkg_data['name'] = basename
            
            # When using --packages, default to state repo version unless --use-latest is specified
            should_use_latest = False if force_packages else use_latest
            if force_packages and use_latest:
                should_use_latest = True
            
            newer_in_x86.append({
                'name': basename,
                'version': x86_data['version'],
                'current_version': target_version,
                'basename': x86_data['pkg_data']['basename'],
                'repo': x86_data['pkg_data']['repo'],
                'depends': x86_data['pkg_data'].get('depends', []),
                'makedepends': x86_data['pkg_data'].get('makedepends', []),
                'provides': x86_data['pkg_data'].get('provides', []),
                'force_latest': should_use_latest,
                'use_aur': bool(basename in aur_packages),
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

def find_strongly_connected_components(graph):
    """
    Find strongly connected components using Tarjan's algorithm.
    Returns list of SCCs, each SCC is a list of nodes.
    """
    index_counter = [0]
    stack = []
    lowlinks = {}
    index = {}
    on_stack = {}
    sccs = []
    
    def strongconnect(node):
        index[node] = index_counter[0]
        lowlinks[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        
        for successor in graph.get(node, []):
            if successor not in index:
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif on_stack.get(successor, False):
                lowlinks[node] = min(lowlinks[node], index[successor])
        
        if lowlinks[node] == index[node]:
            component = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == node:
                    break
            sccs.append(component)
    
    for node in graph:
        if node not in index:
            strongconnect(node)
    
    return sccs

def sort_by_build_order(packages):
    """
    Sort packages by dependency order using topological sort with proper cycle detection.
    """
    from collections import defaultdict, deque
    
    # Create package name to package mapping
    pkg_map = {pkg['name']: pkg for pkg in packages}
    
    # Build provides mapping from packages in the build list
    build_list_provides = {}
    for pkg in packages:
        pkg_name = pkg['name']
        basename = pkg.get('basename', pkg_name)
        build_list_provides[pkg_name] = pkg_name
        build_list_provides[basename] = basename
        
        # Add explicit provides
        for provide in pkg.get('provides', []):
            provide_name = provide.split('=')[0]
            build_list_provides[provide_name] = pkg_name
    
    # Add common split package patterns for packages in build list
    # This handles cases like linux providing linux-headers, linux-docs, etc.
    basenames_in_build = {pkg.get('basename', pkg['name']) for pkg in packages}
    for basename in basenames_in_build:
        # Common split package suffixes
        for suffix in ['-headers', '-docs', '-devel', '-dev']:
            split_name = basename + suffix
            if split_name not in build_list_provides:
                build_list_provides[split_name] = basename
    
    # Build dependency graph - only consider packages in our build list
    graph = defaultdict(set)  # pkg -> set of packages that depend on it
    reverse_graph = defaultdict(set)  # pkg -> set of packages it depends on
    in_degree = defaultdict(int)  # pkg -> number of dependencies
    
    # Initialize in_degree for all packages
    for pkg in packages:
        in_degree[pkg['name']] = 0
    
    # Build the dependency graph
    for pkg in packages:
        pkg_name = pkg['name']
        
        # Collect all dependencies
        all_deps = []
        for dep_type in ['depends', 'makedepends', 'checkdepends']:
            all_deps.extend(pkg.get(dep_type, []))
        
        # Process each dependency
        for dep_str in all_deps:
            # Extract package name (remove version constraints)
            dep_name = dep_str.split('=')[0].split('>')[0].split('<')[0].strip()
            
            # Resolve provides relationships
            provider_pkg = None
            if dep_name in pkg_map:
                provider_pkg = dep_name
            elif dep_name in build_list_provides:
                provider_pkg = build_list_provides[dep_name]
            
            # Only create edge if dependency is also in our build list
            if provider_pkg and provider_pkg in pkg_map and provider_pkg != pkg_name:
                graph[provider_pkg].add(pkg_name)
                reverse_graph[pkg_name].add(provider_pkg)
                in_degree[pkg_name] += 1
    
    # Find strongly connected components (cycles)
    sccs = find_strongly_connected_components(reverse_graph)
    
    # Identify actual cycles (SCCs with more than one node or self-loops)
    cycles = []
    cycle_map = {}  # pkg_name -> cycle_id
    
    for scc in sccs:
        if len(scc) > 1 or (len(scc) == 1 and scc[0] in reverse_graph.get(scc[0], set())):
            cycle_id = len(cycles)
            cycles.append(scc)
            for pkg_name in scc:
                cycle_map[pkg_name] = cycle_id
    
    # Process cycles first, then regular packages
    result = []
    current_stage = 0
    processed_packages = set()
    
    # Process all cycles first
    for cycle_id, cycle_pkgs in enumerate(cycles):
        # Add first build of cycle packages
        for pkg_name in cycle_pkgs:
            pkg = pkg_map[pkg_name].copy()
            pkg['build_stage'] = current_stage
            pkg['cycle_group'] = cycle_id
            pkg['cycle_stage'] = 1
            result.append(pkg)
            processed_packages.add(pkg_name)
        
        # Add second build of cycle packages
        for pkg_name in cycle_pkgs:
            pkg = pkg_map[pkg_name].copy()
            pkg['build_stage'] = current_stage + 1
            pkg['cycle_group'] = cycle_id
            pkg['cycle_stage'] = 2
            result.append(pkg)
        
        current_stage += 2
    
    # Now process remaining packages with topological sort
    remaining_packages = [pkg for pkg in packages if pkg['name'] not in processed_packages]
    if remaining_packages:
        # Build new graph without cycle packages
        remaining_graph = defaultdict(set)
        remaining_in_degree = defaultdict(int)
        
        for pkg in remaining_packages:
            remaining_in_degree[pkg['name']] = 0
        
        for pkg in remaining_packages:
            pkg_name = pkg['name']
            all_deps = []
            for dep_type in ['depends', 'makedepends', 'checkdepends']:
                all_deps.extend(pkg.get(dep_type, []))
            
            for dep_str in all_deps:
                dep_name = dep_str.split('=')[0].split('>')[0].split('<')[0].strip()
                provider_pkg = None
                if dep_name in pkg_map:
                    provider_pkg = dep_name
                elif dep_name in build_list_provides:
                    provider_pkg = build_list_provides[dep_name]
                
                # Create edge if dependency is in remaining packages OR was in a cycle (already built)
                if provider_pkg and provider_pkg in pkg_map and provider_pkg != pkg_name:
                    if provider_pkg in processed_packages:
                        # Dependency was in a cycle, already satisfied
                        continue
                    elif any(p['name'] == provider_pkg for p in remaining_packages):
                        # Dependency is in remaining packages
                        remaining_graph[provider_pkg].add(pkg_name)
                        remaining_in_degree[pkg_name] += 1
        
        # Topological sort for remaining packages
        queue = deque()
        for pkg in remaining_packages:
            if remaining_in_degree[pkg['name']] == 0:
                queue.append((pkg['name'], current_stage))
        
        while queue:
            next_queue = deque()
            stage_packages = []
            
            while queue:
                pkg_name, stage = queue.popleft()
                if stage == current_stage:
                    stage_packages.append(pkg_name)
                else:
                    next_queue.append((pkg_name, stage))
            
            if not stage_packages:
                current_stage += 1
                queue = next_queue
                continue
            
            for pkg_name in stage_packages:
                pkg = pkg_map[pkg_name].copy()
                pkg['build_stage'] = current_stage
                pkg['cycle_group'] = None
                pkg['cycle_stage'] = None
                result.append(pkg)
                
                for dependent in remaining_graph[pkg_name]:
                    remaining_in_degree[dependent] -= 1
                    if remaining_in_degree[dependent] == 0:
                        next_queue.append((dependent, current_stage + 1))
            
            current_stage += 1
            queue = next_queue
    
    # Sort by build stage, then by cycle stage, then by name
    return sorted(result, key=lambda x: (x['build_stage'], x.get('cycle_stage') or 0, x['name']))

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
    parser.add_argument('--force', action='store_true',
                        help='Force rebuild ARCH=any packages (use with --packages)')
    parser.add_argument('--preserve-order', action='store_true',
                        help='Preserve exact order specified in --packages (skip dependency sorting)')
    parser.add_argument('--blacklist', default='blacklist.txt',
                        help='File containing packages to skip (default: blacklist.txt)')
    parser.add_argument('--missing-packages', action='store_true',
                        help=f'Generate list of all packages present in x86_64 but missing from {target_arch}')
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
                newer_packages = fetch_pkgbuild_deps(newer_packages, args.no_update, {})
            
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
        # Load x86_64 packages and target packages for dependency checking
        # Include ARCH=any packages if --force is used
        if args.force:
            from utils import load_packages_with_any, X86_64_MIRROR
            any_urls = [
                f"{X86_64_MIRROR}/core/os/x86_64/core.db",
                f"{X86_64_MIRROR}/extra/os/x86_64/extra.db"
            ]
            all_x86_packages = load_packages_with_any(any_urls, '_x86_64', download=not args.no_update, include_any=True)
        else:
            all_x86_packages = load_x86_64_packages(download=not args.no_update)
        target_packages = load_target_arch_packages(download=not args.no_update)
        
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
                        if not args.force:
                            print(f"WARNING: Package {pkg_name} is ARCH=any and doesn't need rebuilding - skipping")
                            print(f"         Use --force to build anyway")
                            continue  # Skip ARCH=any packages
                        else:
                            print(f"WARNING: Forcing rebuild of ARCH=any package {pkg_name}")
                            # Package should already be in all_x86_packages since we loaded with include_any=True
        x86_packages = filtered_x86_packages
        # Store full package list for dependency resolution
        full_x86_packages = all_x86_packages
    elif not args.aur:
        # Load packages using unified function
        repo_filter = [args.rebuild_repo] if args.rebuild_repo else None
        x86_packages, target_packages = load_packages_unified(download=not args.no_update, x86_repos=repo_filter, target_repos=repo_filter)
        
        # Use same list for dependency resolution
        full_x86_packages = x86_packages
    else:
        print("Skipping x86_64 parsing (using AUR)")
        full_x86_packages = {}
        # For AUR mode, we still need target packages for comparison
        target_arch = get_target_architecture()
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
    
    # Load provides mapping once (doesn't change)
    print("Loading provides mapping from upstream databases...")
    
    # Stage 1: Find outdated packages using .db files (fast comparison)
    newer_packages, skipped_packages, blacklisted_missing, bin_package_warnings = compare_versions(x86_packages, target_packages, args.packages, blacklist, args.missing_packages, args.use_aur_for_packages, args.use_latest, full_x86_packages)
    
    # Handle --rebuild-repo option
    if args.rebuild_repo:
        print(f"Rebuilding all packages from {args.rebuild_repo} repository...")
        repo_packages = []
        toolchain_packages = {'gcc', 'binutils', 'glibc'}
        
        for pkg_name, pkg_data in x86_packages.items():
            if pkg_data['repo'] == args.rebuild_repo:
                if pkg_data['basename'] in toolchain_packages:
                    continue
                    
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
    
    # Stage 2: Parse PKGBUILDs for complete dependency info and find missing deps
    if newer_packages and not args.missing_packages:
        print("Processing PKGBUILDs for complete dependency information...")
        newer_packages = fetch_pkgbuild_deps(newer_packages, args.no_update)
        
        if full_x86_packages:
            print("Checking for missing dependencies (including checkdepends)...")
            missing_deps = find_missing_dependencies(newer_packages, full_x86_packages, target_packages)
            if missing_deps:
                print(f"Found {len(missing_deps)} missing dependencies: {', '.join(sorted(missing_deps))}")
                
                # Track reasons for each missing dependency
                dep_reasons = {}
                for pkg in newer_packages:
                    all_deps = pkg.get('build_depends', pkg.get('depends', [])) + pkg.get('build_makedepends', pkg.get('makedepends', []))
                    if 'build_checkdepends' in pkg:
                        all_deps += pkg['build_checkdepends']
                    elif 'checkdepends' in pkg:
                        all_deps += pkg['checkdepends']
                    
                    for dep in all_deps:
                        dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                        if dep_name in missing_deps:
                            if dep_name not in dep_reasons:
                                dep_reasons[dep_name] = []
                            
                            # Determine dependency type
                            if dep in pkg.get('build_checkdepends', pkg.get('checkdepends', [])):
                                dep_type = "checkdepends"
                            elif dep in pkg.get('build_makedepends', pkg.get('makedepends', [])):
                                dep_type = "makedepends"
                            else:
                                dep_type = "depends"
                            
                            dep_reasons[dep_name].append(f"{dep_type} for {pkg['name']}")
                
                for dep_name in missing_deps:
                    if dep_name in full_x86_packages:
                        pkg = full_x86_packages[dep_name]
                        basename = pkg['basename']
                        
                        is_blacklisted = False
                        for pattern in blacklist:
                            if fnmatch.fnmatch(dep_name, pattern):
                                is_blacklisted = True
                                break
                        
                        if not is_blacklisted and basename not in [p['name'] for p in newer_packages]:
                            reason = ", ".join(dep_reasons.get(dep_name, ["unknown reason"]))
                            newer_packages.append({
                                'name': basename,
                                'version': pkg['version'],
                                'current_version': 'not found',
                                'basename': pkg['basename'],
                                'repo': pkg['repo'],
                                'depends': pkg.get('depends', []),
                                'makedepends': pkg.get('makedepends', []),
                                'provides': pkg.get('provides', []),
                                'force_latest': args.use_latest,
                                'use_aur': False,
                                'added_reason': reason,
                            })
                
                # Parse PKGBUILDs only for newly added dependencies
                added_deps = [dep_name for dep_name in missing_deps if dep_name in full_x86_packages]
                if added_deps:
                    # Create list of only the newly added packages
                    new_packages = [pkg for pkg in newer_packages if pkg['name'] in added_deps]
                    print("Processing PKGBUILDs for missing dependencies...")
                    new_packages_with_deps = fetch_pkgbuild_deps(new_packages, args.no_update)
                    
                    # Update the newer_packages list with the processed new packages
                    for updated_pkg in new_packages_with_deps:
                        for i, pkg in enumerate(newer_packages):
                            if pkg['name'] == updated_pkg['name']:
                                newer_packages[i] = updated_pkg
                                break
                    
                    # Re-filter dependencies for all packages now that we have the complete list
                    print("Re-filtering dependencies with complete package list...")
                    build_list_names = {pkg['name'] for pkg in newer_packages}
                    build_list_basenames = {pkg.get('basename', pkg['name']) for pkg in newer_packages}
                    
                    # Build provides mapping from packages in the build list
                    build_list_provides = {}
                    for pkg in newer_packages:
                        pkg_name = pkg['name']
                        basename = pkg.get('basename', pkg_name)
                        build_list_provides[pkg_name] = basename
                        build_list_provides[basename] = basename
                        for provide in pkg.get('provides', []):
                            provide_name = provide.split('=')[0]
                            build_list_provides[provide_name] = basename
                    
                    # Add common split package patterns for packages in build list
                    basenames_in_build = {pkg.get('basename', pkg['name']) for pkg in newer_packages}
                    for basename in basenames_in_build:
                        for suffix in ['-headers', '-docs', '-devel', '-dev']:
                            split_name = basename + suffix
                            if split_name not in build_list_provides:
                                build_list_provides[split_name] = basename
                    
                    # Add common split package patterns for packages in build list
                    basenames_in_build = {pkg.get('basename', pkg['name']) for pkg in newer_packages}
                    for basename in basenames_in_build:
                        for suffix in ['-headers', '-docs', '-devel', '-dev']:
                            split_name = basename + suffix
                            if split_name not in build_list_provides:
                                build_list_provides[split_name] = basename
                    
                    for pkg in newer_packages:
                        # Filter dependencies for build ordering only
                        for dep_type in ['depends', 'makedepends', 'checkdepends']:
                            if dep_type in pkg:
                                filtered_deps = []
                                for dep in pkg.get(f'build_{dep_type}', pkg.get(dep_type, [])):
                                    dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                                    if (dep_name in build_list_names or 
                                        dep_name in build_list_basenames or
                                        dep_name in build_list_provides):
                                        filtered_deps.append(dep)
                                pkg[dep_type] = filtered_deps
    elif newer_packages:
        # For missing packages mode, still need PKGBUILD parsing
        print("Processing PKGBUILDs for dependency information...")
        newer_packages = fetch_pkgbuild_deps(newer_packages, args.no_update)
    
    # Report results
    if args.missing_packages and blacklisted_missing:
        print(f"Found {len(newer_packages)} missing packages, {len(blacklisted_missing)} blacklisted")
    elif args.missing_packages:
        print(f"Found {len(newer_packages)} missing packages")
    
    if bin_package_warnings:
        print("\n" + "="*SEPARATOR_WIDTH)
        for warning in bin_package_warnings:
            print(warning)
        print("="*SEPARATOR_WIDTH)
    
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
                if pkg.get('version') == pkg.get('current_version'):
                    rebuild_packages.append(pkg_name)
                else:
                    update_packages.append(pkg_name)
        
        rebuild_list = ', '.join(f"\033[1m{pkg}\033[0m" for pkg in rebuild_packages) if rebuild_packages else ""
        if rebuild_list:
            print(f"Found {len(newer_packages)} packages (including rebuilds: {rebuild_list})")
        else:
            print(f"Found {len(newer_packages)} packages")
    else:
        if args.missing_packages:
            print(f"Found {len(newer_packages)} missing packages")
        else:
            # Count outdated vs new packages
            outdated_count = len([pkg for pkg in newer_packages if pkg.get('current_version', 'unknown') not in ['not found', 'unknown']])
            new_count = len([pkg for pkg in newer_packages if pkg.get('current_version') == 'not found'])
            
            if outdated_count > 0 and new_count > 0:
                print(f"Found {outdated_count} packages where x86_64 is newer and {new_count} missing package{'s' if new_count != 1 else ''}.")
            elif outdated_count > 0:
                print(f"Found {outdated_count} packages where x86_64 is newer")
            elif new_count > 0:
                print(f"Found {new_count} missing package{'s' if new_count != 1 else ''}")
            else:
                print(f"Found {len(newer_packages)} packages")
    
    # Categorize packages by version change type
    if newer_packages:
        from utils import is_version_newer
        
        upgrades = []
        rebuilds = []
        downgrades = []
        missing = []
        
        for pkg in newer_packages:
            current = pkg.get('current_version', 'unknown')
            new = pkg['version']
            
            if current == 'not found':
                missing.append(pkg)
            elif current == 'unknown':
                rebuilds.append(pkg)
            elif current == new:
                rebuilds.append(pkg)
            elif is_version_newer(current, new):
                upgrades.append(pkg)
            else:
                downgrades.append(pkg)
        
        # Show upgrades
        if upgrades:
            print("\nUpgrades:")
            for pkg in upgrades:
                current = pkg.get('current_version', 'unknown')
                new = pkg['version']
                print(f"  {pkg['name']}: {current} → {new}")
        
        # Show missing packages
        if missing:
            print("\nNew packages:")
            for pkg in missing:
                reason = pkg.get('added_reason', '')
                if reason:
                    print(f"  {pkg['name']}: {pkg['version']} ({reason})")
                else:
                    print(f"  {pkg['name']}: {pkg['version']}")
        
        # Show rebuilds
        if rebuilds:
            print("\nRebuilds:")
            for pkg in rebuilds:
                print(f"  {pkg['name']}: {pkg['version']}")
        
        # Show downgrades
        if downgrades:
            print("\nDowngrades:")
            for pkg in downgrades:
                current = pkg.get('current_version', 'unknown')
                new = pkg['version']
                print(f"  {pkg['name']}: {current} → {new}")
    
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
    
    print("Complete!")
