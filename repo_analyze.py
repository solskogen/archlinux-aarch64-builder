#!/usr/bin/env python3

import argparse
from pathlib import Path
from packaging import version
import tarfile
import fnmatch
from multiprocessing import Pool, cpu_count
from functools import partial



from utils import load_blacklist, parse_database_file, X86_64_MIRROR, get_target_architecture, load_target_arch_packages, load_packages_with_any, config, load_all_packages_parallel

def build_provides_chunk(packages_chunk):
    """Build provides mapping for a chunk of packages"""
    provides = {}
    for pkg_name, pkg_data in packages_chunk:
        for provide in pkg_data.get('provides', []):
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
            provides[provide_name] = pkg_name
    return provides

def build_provides_parallel(packages):
    """Build provides mapping using multiple cores"""
    if len(packages) < 1000:  # Use single thread for small datasets
        provides = {}
        for pkg_name, pkg_data in packages.items():
            for provide in pkg_data.get('provides', []):
                provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
                provides[provide_name] = pkg_name
        return provides
    
    # Split packages into chunks for parallel processing
    items = list(packages.items())
    chunk_size = max(100, len(items) // cpu_count())
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    
    with Pool() as pool:
        results = pool.map(build_provides_chunk, chunks)
    
    # Merge results
    provides = {}
    for result in results:
        provides.update(result)
    
    return provides

def main():
    target_arch = get_target_architecture()
    
    parser = argparse.ArgumentParser(description=f'Analyze differences between x86_64 and {target_arch} repositories')
    parser.add_argument('--blacklist', help='Blacklist file (default: blacklist.txt)')
    parser.add_argument('--use-existing-db', action='store_true', help='Use existing database files instead of downloading')
    parser.add_argument('--missing-pkgbase', action='store_true', help='Print missing pkgbase names (space delimited)')
    parser.add_argument('--outdated-any', action='store_true', help='Show outdated any packages')
    parser.add_argument('--missing-any', action='store_true', help='Show missing any packages')
    parser.add_argument('--repo-issues', action='store_true', help='Show repository inconsistencies and duplicates')
    parser.add_argument('--repo-mismatches', action='store_true', dest='repo_issues', help=argparse.SUPPRESS)  # Legacy alias
    parser.add_argument('--target-newer', action='store_true', help=f'Show packages where {target_arch} is newer')
    parser.add_argument('--target-only', action='store_true', help=f'Show {target_arch} only packages')
    parser.add_argument('--target-duplicates', action='store_true', dest='repo_issues', help=argparse.SUPPRESS)  # Legacy alias
    # Compatibility aliases
    parser.add_argument('--arm-newer', action='store_true', dest='target_newer', help=argparse.SUPPRESS)
    parser.add_argument('--arm-only', action='store_true', dest='target_only', help=argparse.SUPPRESS)
    parser.add_argument('--arm-duplicates', action='store_true', dest='target_duplicates', help=argparse.SUPPRESS)
    args = parser.parse_args()
    
    # Load blacklist
    blacklist_file = args.blacklist or 'blacklist.txt'
    blacklist = load_blacklist(blacklist_file) if Path(blacklist_file).exists() else []
    
    # Load packages using shared parallel function (include any packages for analysis)
    print("Loading packages...")
    x86_packages, target_packages = load_all_packages_parallel(download=not args.use_existing_db, include_any=True)
    
    # Group by basename (optimized)
    print("Grouping packages by basename...")
    x86_bases = {}
    x86_by_basename = {}  # basename -> [pkg_names]
    for pkg_name, pkg_data in x86_packages.items():
        basename = pkg_data['basename']
        x86_bases[basename] = pkg_data
        if basename not in x86_by_basename:
            x86_by_basename[basename] = []
        x86_by_basename[basename].append(pkg_name)
    
    target_bases = {}
    target_by_basename = {}  # basename -> [pkg_names]
    target_repo_count = {}
    for pkg_name, pkg_data in target_packages.items():
        basename = pkg_data['basename']
        target_bases[basename] = pkg_data
        if basename not in target_by_basename:
            target_by_basename[basename] = []
        target_by_basename[basename].append(pkg_name)
        # Track which repos each basename appears in
        if basename not in target_repo_count:
            target_repo_count[basename] = set()
        target_repo_count[basename].add(pkg_data['repo'])
    
    # Find packages in both core and extra (target architecture)
    for basename, repos in target_repo_count.items():
        if len(repos) > 1:
            target_duplicates.append(f"{basename}: present in {', '.join(sorted(repos))}")
    
    # Build provides lookup for x86_64
    print(f"Building x86_64 provides mapping ({len(x86_packages)} packages)...")
    x86_provides = {}
    for i, (pkg_name, pkg_data) in enumerate(x86_packages.items()):
        for provide in pkg_data.get('provides', []):
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
            x86_provides[provide_name] = pkg_name
    
    # Build provides lookup for target architecture
    print(f"Building {target_arch} provides mapping ({len(target_packages)} packages)...")
    target_provides = {}
    for i, (pkg_name, pkg_data) in enumerate(target_packages.items()):
        for provide in pkg_data.get('provides', []):
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
            target_provides[provide_name] = pkg_name
    
    repo_issues = []
    target_newer = []
    target_only = []
    any_outdated = []
    any_missing = []
    missing_pkgbase = []
    package_name_mismatches = []
    
    # Check for package name mismatches (optimized)
    print(f"Checking package name mismatches ({len(x86_bases)} basenames)...")
    for i, basename in enumerate(x86_bases):
        if basename in target_bases:
            # Use pre-grouped package names (O(1) lookup instead of O(n) iteration)
            x86_pkg_names = set(x86_by_basename[basename])
            target_pkg_names = set(target_by_basename[basename])
            
            # Find packages that exist in one arch but not the other
            x86_only = x86_pkg_names - target_pkg_names
            target_only_names = target_pkg_names - x86_pkg_names
            
            # Filter out packages that are provided by other packages
            x86_only_filtered = set()
            for pkg_name in x86_only:
                # Check if this package is provided by any target package
                if pkg_name not in target_provides:
                    x86_only_filtered.add(pkg_name)
            
            target_only_filtered = set()
            for pkg_name in target_only_names:
                # Check if this package is provided by any x86_64 package
                if pkg_name not in x86_provides:
                    target_only_filtered.add(pkg_name)
            
            if x86_only_filtered or target_only_filtered:
                parts = []
                if x86_only_filtered:
                    parts.append(f"x86_64 has {', '.join(sorted(x86_only_filtered))}")
                if target_only_filtered:
                    # Add filenames for target-only packages
                    target_only_with_files = []
                    for pkg_name in sorted(target_only_filtered):
                        if pkg_name in target_packages:
                            pkg_data = target_packages[pkg_name]
                            filename = pkg_data.get('filename', f"{pkg_name}-{pkg_data.get('version', 'unknown')}-{target_arch}.pkg.tar.zst")
                            target_only_with_files.append(f"{pkg_name} ({filename})")
                        else:
                            target_only_with_files.append(pkg_name)
                    parts.append(f"{target_arch} has {', '.join(target_only_with_files)}")
                package_name_mismatches.append(f"{basename}: {', '.join(parts)}")
    
    # Check for packages in multiple repositories (same architecture)
    for basename, repos in target_repo_count.items():
        if len(repos) > 1:
            repo_issues.append(f"{basename}: present in {', '.join(sorted(repos))} on {target_arch}")
    
    # Find missing pkgbase in target architecture
    print(f"Finding missing pkgbase ({len(x86_bases)} to check)...")
    for i, basename in enumerate(x86_bases):
        if basename not in target_bases:
            # Check if basename matches any blacklist pattern
            is_blacklisted = False
            for pattern in blacklist:
                if fnmatch.fnmatch(basename, pattern):
                    is_blacklisted = True
                    break
            
            # Check if any dependencies are blacklisted
            if not is_blacklisted:
                x86_data = x86_bases[basename]
                all_deps = x86_data.get('depends', []) + x86_data.get('makedepends', [])
                for dep in all_deps:
                    dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
                    for pattern in blacklist:
                        if fnmatch.fnmatch(dep_name, pattern):
                            is_blacklisted = True
                            break
                    if is_blacklisted:
                        break
            
            if not is_blacklisted:
                missing_pkgbase.append(basename)
    
    # Check AArch64 packages
    print(f"Checking {target_arch} packages ({len(target_bases)} basenames)...")
    for i, (basename, target_data) in enumerate(target_bases.items()):
        if basename in x86_bases:
            x86_data = x86_bases[basename]
            
            # Check for outdated any packages - use pre-grouped data
            for pkg_name in target_by_basename[basename]:
                pkg_data = target_packages[pkg_name]
                if pkg_data.get('arch') == 'any' or pkg_data.get('filename', '').endswith('any.pkg.tar.zst'):
                    try:
                        if version.parse(pkg_data['version']) < version.parse(x86_data['version']):
                            any_outdated.append(f"{pkg_name}: {target_arch}={pkg_data['version']}, x86_64={x86_data['version']}")
                    except:
                        pass
            
            # Check repo mismatch
            if target_data['repo'] != x86_data['repo']:
                repo_issues.append(f"{basename}: {target_arch} in {target_data['repo']}, x86_64 in {x86_data['repo']}")
            
            # Check if ARM newer
            try:
                from utils import is_version_newer
                if is_version_newer(x86_data['version'], target_data['version']):
                    target_newer.append(f"{basename}: {target_arch} {target_data['version']} > x86_64 {x86_data['version']}")
            except:
                pass
        else:
            # Check if this package is provided by something in x86_64
            is_provided = False
            for pkg_name, pkg_data in target_packages.items():
                if pkg_data['basename'] == basename:
                    # Check if this package name or any of its provides exist in x86_64
                    if pkg_name in x86_packages or pkg_name in x86_provides:
                        is_provided = True
                        break
                    # Check if any of the provides from this package exist in x86_64
                    for provide in pkg_data.get('provides', []):
                        provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
                        if provide_name in x86_packages or provide_name in x86_provides:
                            is_provided = True
                            break
                    if is_provided:
                        break
            
            # Package only in AArch64 - show all regardless of provides
            # Find all package names for this basename
            pkg_names = [name for name, data in target_packages.items() if data['basename'] == basename]
            if len(pkg_names) == 1 and pkg_names[0] == basename:
                # Single package with same name as basename
                arch = target_data.get('arch', target_arch)
                if isinstance(arch, set):
                    arch = list(arch)[0] if arch else target_arch
                filename = f"{basename}-{target_data['version']}-{arch}.pkg.tar.zst"
                
                # Check if this is a -bin package and compare with x86_64 counterpart
                version_info = ""
                if basename.endswith('-bin'):
                    counterpart = basename[:-4]  # Remove '-bin' suffix
                    x86_counterpart = None
                    x86_version = None
                    
                    # First check direct package name match
                    if counterpart in x86_packages:
                        x86_counterpart = counterpart
                        x86_version = x86_packages[counterpart]['version']
                    else:
                        # Check if this -bin package provides something that exists in x86_64
                        for provide in target_data.get('provides', []):
                            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
                            if provide_name in x86_packages:
                                x86_counterpart = provide_name
                                x86_version = x86_packages[provide_name]['version']
                                break
                    
                    if x86_version:
                        # For -bin packages, compare only pkgver (ignore pkgrel)
                        def get_pkgver(version_str):
                            # Split version-release, return only version part
                            return version_str.rsplit('-', 1)[0]
                        
                        aarch64_pkgver = get_pkgver(target_data['version'])
                        x86_pkgver = get_pkgver(x86_version)
                        
                        if aarch64_pkgver == x86_pkgver:
                            version_info = f" \033[32m[matches x86_64 {x86_counterpart}]\033[0m"
                        else:
                            # Compare versions to determine if newer or older
                            from packaging import version
                            try:
                                if version.parse(aarch64_pkgver) > version.parse(x86_pkgver):
                                    version_info = f" \033[36m[NEWER than x86_64 {x86_counterpart}: {x86_version}]\033[0m"
                                else:
                                    version_info = f" \033[31m[OUTDATED - x86_64 {x86_counterpart}: {x86_version}]\033[0m"
                            except:
                                # Fallback if version parsing fails
                                version_info = f" [x86_64 {x86_counterpart}: {x86_version}]"
                
                # Color code for aarch64-only packages in extra/core repos (should not exist)
                line = f"{basename}: {target_data['version']} ({target_data['repo']}) (file: {filename}){version_info}"
                if target_data['repo'] in ['core', 'extra']:
                    line = f"\033[31m{line}\033[0m"
                target_only.append(line)
            else:
                # Multiple packages - list each package individually
                arch = target_data.get('arch', target_arch)
                if isinstance(arch, set):
                    arch = list(arch)[0] if arch else target_arch
                
                for pkg_name in pkg_names:
                    filename = f"{pkg_name}-{target_data['version']}-{arch}.pkg.tar.zst"
                    target_only.append(f"{pkg_name}: {target_data['version']} ({target_data['repo']}) (file: {filename})")
    
    # Check for missing 'any' packages in AArch64
    for basename, x86_data in x86_bases.items():
        if basename not in target_bases:
            # Check if any individual packages for this basename are 'any' architecture
            for pkg_name, pkg_data in x86_packages.items():
                if pkg_data['basename'] == basename:
                    if pkg_data.get('arch') == 'any' or pkg_data.get('filename', '').endswith('any.pkg.tar.zst'):
                        any_missing.append(f"{pkg_name}: x86_64={pkg_data['version']} ({pkg_data['repo']})")
                        break
    
    # Output based on command line options
    if args.missing_pkgbase:
        print(' '.join(sorted(missing_pkgbase)))
        return
    
    # If no specific options, show all except missing-pkgbase (default behavior)
    show_all = not any([args.outdated_any, args.missing_any, args.repo_issues, args.target_newer, args.target_only])
    
    if show_all and package_name_mismatches:
        print(f"\nPackage Name Mismatches ({len(package_name_mismatches)}):")
        for mismatch in sorted(package_name_mismatches):
            print(f"  {mismatch}")
    
    if show_all or args.outdated_any:
        if any_outdated:
            print(f"\nOutdated 'any' Packages in AArch64 ({len(any_outdated)}):")
            for pkg in sorted(any_outdated):
                print(f"  {pkg}")
        else:
            print(f"\nOutdated 'any' Packages in AArch64: None found")
    
    if show_all or args.missing_any:
        if any_missing:
            print(f"\nMissing 'any' Packages in AArch64 ({len(any_missing)}):")
            for pkg in sorted(any_missing):
                print(f"  {pkg}")
        else:
            print(f"\nMissing 'any' Packages in AArch64: None found")
    
    if show_all or args.repo_issues:
        if repo_issues:
            print(f"\nRepository Issues ({len(repo_issues)}):")
            for issue in sorted(repo_issues):
                print(f"  {issue}")
        else:
            print(f"\nRepository Issues: None found")
    
    if show_all or args.target_only:
        if target_only:
            print(f"\n{target_arch} Only Packages ({len(target_only)}):")
            # Sort with -bin packages at the end
            def sort_key(pkg):
                name = pkg.split(':')[0]
                return (name.endswith('-bin'), name)
            
            for pkg in sorted(target_only, key=sort_key):
                print(f"  {pkg}")
        else:
            print(f"\n{target_arch} Only Packages: None found")
    
    if show_all or args.target_newer:
        if target_newer:
            print(f"\n{target_arch} Newer Versions ({len(target_newer)}):")
            for pkg in sorted(target_newer):
                print(f"  {pkg}")
        else:
            print(f"\n{target_arch} Newer Versions: None found")
    
    if show_all and not repo_issues and not target_newer and not target_only and not any_outdated and not any_missing and not package_name_mismatches:
        print("No issues found")

if __name__ == "__main__":
    main()
