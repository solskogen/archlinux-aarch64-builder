#!/bin/bash
'''exec' python3 -B "$0" "$@"
' '''
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
from pathlib import Path
from packaging import version
from utils import load_blacklist

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
    """Extract dependencies from PKGBUILD file"""
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

def fetch_pkgbuild_deps(packages_to_build, no_update=False, arm_packages=None):
    """Fetch PKGBUILDs for packages that need building and extract full dependencies"""
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
        pkgbuild_dir = Path("pkgbuilds") / name
        pkgbuild_path = pkgbuild_dir / "PKGBUILD"
        
        # Fetch or update PKGBUILD (skip if no_update is True)
        if no_update:
            print(f"[{i}/{total}] Using existing PKGBUILD for {name}")
        elif not pkgbuild_path.exists():
            # Need to clone the repository
            try:
                if pkg.get('use_aur', False):
                    print(f"[{i}/{total}] Fetching AUR PKGBUILD for {name}...")
                    result = subprocess.run(["git", "clone", f"https://aur.archlinux.org/{name}.git", name], 
                                         cwd="pkgbuilds", check=True, 
                                         capture_output=True, text=True)
                else:
                    print(f"[{i}/{total}] Fetching PKGBUILD for {name}...")
                    # Use --switch to get the correct version tag only if not using latest
                    if pkg.get('force_latest', False):
                        # Get latest commit from main branch
                        result = subprocess.run(["pkgctl", "repo", "clone", name], 
                                             cwd="pkgbuilds", check=True, 
                                             capture_output=True, text=True)
                    else:
                        # Use --switch to get the specific version tag
                        version_tag = pkg['version']
                        result = subprocess.run(["pkgctl", "repo", "clone", name, f"--switch={version_tag}"], 
                                             cwd="pkgbuilds", check=True, 
                                             capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to fetch PKGBUILD for {name}: {e.stderr.strip() if e.stderr else 'Unknown error'}")
                continue
        elif not no_update:
            # Repository exists, ensure we're on the correct version (skip if no_update)
            pkg_repo_dir = Path("pkgbuilds") / name
            if pkg_repo_dir.exists() and not pkg.get('use_aur', False):
                try:
                    if pkg.get('force_latest', False):
                        print(f"[{i}/{total}] Switching {name} to latest commit...")
                        subprocess.run(["git", "checkout", "main"], cwd=pkg_repo_dir, check=True, capture_output=True)
                        subprocess.run(["git", "pull"], cwd=pkg_repo_dir, check=True, capture_output=True)
                    else:
                        print(f"[{i}/{total}] Switching {name} to version {pkg['version']}...")
                        version_tag = pkg['version']
                        subprocess.run(["git", "fetch", "--tags"], cwd=pkg_repo_dir, check=True, capture_output=True)
                        # Try different tag formats - replace : with - for git tags
                        git_version_tag = version_tag.replace(':', '-')
                        tag_formats = [git_version_tag, version_tag, f"v{git_version_tag}", f"{name}-{git_version_tag}"]
                        checkout_success = False
                        for tag_format in tag_formats:
                            try:
                                subprocess.run(["git", "checkout", tag_format], cwd=pkg_repo_dir, check=True, capture_output=True)
                                checkout_success = True
                                break
                            except subprocess.CalledProcessError:
                                continue
                        
                        if not checkout_success:
                            print(f"Warning: Could not find tag for {name} version {version_tag}, using latest commit")
                            subprocess.run(["git", "checkout", "main"], cwd=pkg_repo_dir, check=True, capture_output=True)
                            subprocess.run(["git", "pull"], cwd=pkg_repo_dir, check=True, capture_output=True)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to switch {name} to correct version: {e}")
            else:
                print(f"[{i}/{total}] Using existing PKGBUILD for {name}")
        
        # Parse dependencies from existing PKGBUILD
        if pkgbuild_path.exists():
            deps = parse_pkgbuild_deps(pkgbuild_path)
            if deps['depends'] or deps['makedepends']:
                print(f"DEBUG: Found dependencies for {name}: depends={deps['depends'][:3]}{'...' if len(deps['depends']) > 3 else ''}, makedepends={deps['makedepends'][:3]}{'...' if len(deps['makedepends']) > 3 else ''}")
            pkg.update(deps)
    
    # Return combined list with blacklisted packages (unchanged) and fetched packages (with updated deps)
    all_packages = packages_to_fetch + blacklisted_packages
    
    # Build provides mapping from ARM packages database for deduplication
    provides_map = {}
    if arm_packages:
        for pkg_name, pkg_data in arm_packages.items():
            basename = pkg_data.get('basename', pkg_name)
            provides_map[pkg_name] = basename
            for provide in pkg_data.get('provides', []):
                provide_name = provide.split('=')[0]
                provides_map[provide_name] = basename
    
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

def compare_versions(x86_packages, arm_packages, force_packages=None, blacklist=None, missing_packages_mode=False, use_aur=False, use_latest=False):
    if force_packages is None:
        force_packages = set()
    else:
        force_packages = set(force_packages)
    
    if blacklist is None:
        blacklist = set()
    
    # Bootstrap-only packages (excluded from normal builds but not from bootstrap detection)
    bootstrap_only = set(['linux-api-headers', 'glibc', 'binutils', 'gcc'])
    
    skipped_packages = []
    blacklisted_missing = []  # Track blacklisted missing packages
    
    # Group packages by basename
    x86_bases = {}
    arm_bases = {}
    
    # Build provides mapping for ARM packages
    arm_provides = {}
    for name, pkg in arm_packages.items():
        arm_provides[name] = pkg
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
        # But first check if they have newer versions to warn about bootstrap
        if basename in bootstrap_only and not force_packages:
            has_newer_version = False
            if basename in arm_bases:
                arm_version = arm_bases[basename]['version']
                try:
                    # Check if x86_64 has newer version
                    x86_ver_str = x86_data['version']
                    arm_ver_str = arm_version
                    
                    # Extract epoch if present
                    if ':' in x86_ver_str:
                        x86_epoch, x86_ver_str = x86_ver_str.split(':', 1)
                    else:
                        x86_epoch = '0'
                        
                    if ':' in arm_ver_str:
                        arm_epoch, arm_ver_str = arm_ver_str.split(':', 1)
                    else:
                        arm_epoch = '0'
                    
                    # Compare epochs first
                    if int(x86_epoch) != int(arm_epoch):
                        has_newer_version = int(x86_epoch) > int(arm_epoch)
                    else:
                        # Same epoch, compare versions
                        try:
                            x86_ver = version.parse(x86_ver_str)
                            arm_ver = version.parse(arm_ver_str)
                            has_newer_version = x86_ver > arm_ver
                        except Exception:
                            # Fallback: try to extract numeric parts for comparison
                            import re
                            x86_nums = re.findall(r'\d+', x86_ver_str)
                            arm_nums = re.findall(r'\d+', arm_ver_str)
                            
                            # Compare numeric sequences
                            for i in range(min(len(x86_nums), len(arm_nums))):
                                x86_num = int(x86_nums[i])
                                arm_num = int(arm_nums[i])
                                if x86_num != arm_num:
                                    has_newer_version = x86_num > arm_num
                                    break
                            else:
                                # If all compared numbers are equal, compare by string length/content
                                has_newer_version = x86_ver_str > arm_ver_str
                except Exception:
                    pass
            else:
                # Package missing from aarch64
                has_newer_version = True
            
            if has_newer_version:
                skipped_packages.append(f"{basename} (bootstrap-only package - newer version available, run bootstrap script)")
            # Don't add to skipped list if no newer version
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
            arm_version = arm_bases[basename]['version']
            try:
                # Handle epoch versions (e.g., "1:1.0-1")
                x86_ver_str = x86_data['version']
                arm_ver_str = arm_version
                
                # Extract epoch if present
                if ':' in x86_ver_str:
                    x86_epoch, x86_ver_str = x86_ver_str.split(':', 1)
                else:
                    x86_epoch = '0'
                    
                if ':' in arm_ver_str:
                    arm_epoch, arm_ver_str = arm_ver_str.split(':', 1)
                else:
                    arm_epoch = '0'
                
                # Compare epochs first
                if int(x86_epoch) != int(arm_epoch):
                    should_include = int(x86_epoch) > int(arm_epoch)
                else:
                    # Same epoch, compare versions
                    try:
                        x86_ver = version.parse(x86_ver_str)
                        arm_ver = version.parse(arm_ver_str)
                        should_include = x86_ver > arm_ver
                    except Exception:
                        # Fallback: try to extract numeric parts for comparison
                        import re
                        x86_nums = re.findall(r'\d+', x86_ver_str)
                        arm_nums = re.findall(r'\d+', arm_ver_str)
                        
                        # Compare numeric sequences
                        should_include = False
                        for i in range(min(len(x86_nums), len(arm_nums))):
                            x86_num = int(x86_nums[i])
                            arm_num = int(arm_nums[i])
                            if x86_num != arm_num:
                                should_include = x86_num > arm_num
                                break
                        else:
                            # If all compared numbers are equal, compare by string
                            should_include = x86_ver_str > arm_ver_str
            except Exception as e:
                print(f"Warning: Failed to compare versions for {basename}: {e}")
                should_include = False
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
                'force_latest': should_use_latest,
                'use_aur': bool(use_aur and force_packages and basename in force_packages),
                **pkg_data
            })
    
    # Find and add missing dependencies (always enabled except in missing_packages_mode)
    if not missing_packages_mode:
        missing_deps = find_missing_dependencies(newer_in_x86, x86_packages, arm_packages)
        for dep_name in missing_deps:
            if dep_name in x86_packages:
                pkg = x86_packages[dep_name]
                basename = pkg['basename']
                if basename not in [p['name'] for p in newer_in_x86]:
                    newer_in_x86.append({
                        'name': basename,
                        'force_latest': use_latest,
                        'use_aur': False,
                        **pkg
                    })
    
    return newer_in_x86, skipped_packages, blacklisted_missing

def sort_by_build_order(packages):
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
    parser = argparse.ArgumentParser(description='Generate build list by comparing Arch Linux package versions between x86_64 and AArch64 repositories')
    parser.add_argument('--arm-urls', nargs='+',
                        default=["https://arch-linux-repo.drzee.net/arch/core/os/aarch64/core.db",
                                 "https://arch-linux-repo.drzee.net/arch/extra/os/aarch64/extra.db"],
                        help='URLs for AArch64 repository databases')
    parser.add_argument('--packages', nargs='+',
                        help='Force rebuild specific packages by name')
    parser.add_argument('--aur', action='store_true',
                        help='Use AUR as source for packages specified with --packages')
    parser.add_argument('--blacklist', default='blacklist.txt',
                        help='File containing packages to skip (default: blacklist.txt)')
    parser.add_argument('--missing-packages', action='store_true',
                        help='Generate list of all packages present in x86_64 but missing from aarch64')
    parser.add_argument('--rebuild-repo', choices=['core', 'extra'],
                        help='Rebuild all packages from specified repository regardless of version')
    parser.add_argument('--no-update', action='store_true',
                       help='Skip updating state repository and PKGBUILDs (use existing versions)')
    parser.add_argument('--use-latest', action='store_true',
                        help='Use latest git commit of package source instead of version tag when building')
    
    args = parser.parse_args()
    
    # Always use state repository and fetch PKGBUILDs (now default behavior)
    if not args.no_update:
        update_state_repo()
    
    x86_packages = {}
    if args.packages and not args.aur:
        print(f"Looking up versions for {len(args.packages)} specified packages...")
        # Only read the specific package files we need
        for pkg_name in args.packages:
            found = False
            for repo in ["core", "extra"]:
                pkg_file = Path("state") / f"{repo}-x86_64" / pkg_name
                if pkg_file.exists():
                    try:
                        line = pkg_file.read_text().strip()
                        parts = line.split()
                        if len(parts) >= 2:
                            x86_packages[pkg_name] = {
                                'name': pkg_name,
                                'basename': pkg_name,
                                'version': parts[1],
                                'depends': [],
                                'makedepends': [],
                                'provides': [],
                                'repo': repo
                            }
                            found = True
                            break
                    except Exception as e:
                        print(f"Warning: Failed to parse {pkg_file}: {e}")
            if not found:
                print(f"Warning: Package {pkg_name} not found in x86_64 repositories")
    elif not args.aur:
        print("Parsing x86_64 packages from state...")
        # Track packages by basename to detect duplicates across repositories
        package_basenames = {}
        
        # Only parse the requested repository if --rebuild-repo is specified
        if args.rebuild_repo:
            repos_to_parse = [args.rebuild_repo]
        else:
            repos_to_parse = ["core", "extra"]
        
        for repo in repos_to_parse:
            packages = parse_state_repo(repo)
            
            # Check for basenames that exist in multiple repositories
            for pkg_name, pkg_data in packages.items():
                basename = pkg_data['basename']
                if basename in package_basenames:
                    print(f"ERROR: Package basename '{basename}' found in both {package_basenames[basename]} and {repo} repositories")
                    sys.exit(1)
                package_basenames[basename] = repo
            
            x86_packages.update(packages)
    else:
        print("Skipping x86_64 parsing (using AUR)")
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
    
    # Always download ARM databases for provides information
    print("Downloading aarch64 databases for provides information...")
    _, arm_files = download_dbs([], args.arm_urls)
    
    print("Parsing aarch64 packages...")
    arm_packages = {}
    duplicates = []
    for i, db_file in enumerate(arm_files):
        repo_name = "core" if "core" in args.arm_urls[i] else "extra"
        packages = extract_packages(db_file, repo_name)
        # Check for duplicates across ARM repositories
        for pkg_name, pkg_data in packages.items():
            if pkg_name in arm_packages:
                # Check where it belongs according to x86_64
                x86_repo = "NOT FOUND"
                if pkg_name in x86_packages:
                    x86_repo = x86_packages[pkg_name]['repo']
                
                duplicates.append({
                    'name': pkg_name,
                    'first_repo': arm_packages[pkg_name]['repo'],
                    'first_version': arm_packages[pkg_name]['version'],
                    'second_repo': repo_name,
                    'second_version': pkg_data['version'],
                    'x86_repo': x86_repo
                })
            else:
                arm_packages[pkg_name] = pkg_data
        print(f"Found {len(packages)} packages in {args.arm_urls[i]}")
    
    if duplicates and not (args.packages or args.rebuild_repo):
        print(f"\n{'='*80}")
        print(f"WARNING: Found {len(duplicates)} duplicate packages in ARM repositories!")
        print(f"{'='*80}")
        for dup in duplicates:
            print(f"  Package '{dup['name']}':")
            print(f"    {dup['first_repo']}: {dup['first_version']}")
            print(f"    {dup['second_repo']}: {dup['second_version']}")
            print(f"    x86_64 has it in: {dup['x86_repo']}")
        print(f"{'='*80}")
        print("CONTINUING WITH FIRST OCCURRENCE OF EACH DUPLICATE...")
        print(f"{'='*80}\n")
    
    print("Comparing versions...")
    # Skip blacklist entirely when using --packages
    if args.packages:
        blacklist = []
        print("Ignoring blacklist (using --packages)")
    else:
        blacklist = load_blacklist(args.blacklist)
        if blacklist:
            print(f"Loaded blacklist with {len(blacklist)} packages")
    
    newer_packages, skipped_packages, blacklisted_missing = compare_versions(x86_packages, arm_packages, args.packages, blacklist, args.missing_packages, args.aur, args.use_latest)
    
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
        print("Fetching PKGBUILDs for complete dependency information...")
        newer_packages = fetch_pkgbuild_deps(newer_packages, args.no_update, arm_packages)
    
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
        print(f"Found {len(newer_packages)} packages (including forced: {', '.join(args.packages)})")
    else:
        print(f"Found {len(newer_packages)} packages where x86_64 is newer")
    
    print("Sorting by build order...")
    sorted_packages = sort_by_build_order(newer_packages)
    
    print("Writing results to packages_to_build.json...")
    output_data = {
        "_command": " ".join(sys.argv),
        "_timestamp": datetime.datetime.now().isoformat(),
        "packages": sorted_packages
    }
    with open("packages_to_build.json", "w") as f:
        json.dump(output_data, f, indent=2)
    
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
            print(f"\nBuild Statistics:")
            print(f"Total packages to build: {len(buildable_packages)}")
            if len(sorted_packages) > len(buildable_packages):
                blacklisted_requested = len(sorted_packages) - len(buildable_packages)
                print(f"Skipped {blacklisted_requested} blacklisted packages marked for upgrade:")
                for pkg in sorted_packages:
                    if pkg.get('skip', 0) == 1:
                        print(f"  - {pkg['name']} ({pkg.get('blacklist_reason', 'blacklisted')})")
            if skipped_packages:
                print(f"Skipped {len(skipped_packages)} total blacklisted packages")
            print(f"Total build stages: {max_stage + 1}")
            print(f"Packages per stage:")
            for stage in sorted(stage_counts.keys()):
                print(f"  Stage {stage}: {stage_counts[stage]} packages")
    
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
