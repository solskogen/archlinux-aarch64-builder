#!/usr/bin/env python3

import argparse
from pathlib import Path
from packaging import version
import tarfile
import fnmatch

def load_packages_with_any(urls, arch_suffix):
    """Load packages including ARCH=any packages"""
    import subprocess
    packages = {}
    
    for url in urls:
        try:
            db_filename = url.split('/')[-1].replace('.db', f'{arch_suffix}.db')
            
            print(f"Downloading {db_filename}...")
            subprocess.run(["wget", "-q", "-O", db_filename, url], check=True)
            
            repo_name = url.split('/')[-4]
            print(f"Parsing {db_filename}...")
            repo_packages = parse_database_file(db_filename, include_any=True)
            
            for name, pkg in repo_packages.items():
                pkg['repo'] = repo_name
                packages[name] = pkg
                
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to download {url}: {e}")
        except Exception as e:
            print(f"Warning: Failed to parse {db_filename}: {e}")
    
    return packages

from utils import load_blacklist, parse_database_file, X86_64_MIRROR

def main():
    parser = argparse.ArgumentParser(description='Analyze repository differences')
    parser.add_argument('--blacklist', help='Blacklist file (default: blacklist.txt)')
    parser.add_argument('--missing-pkgbase', action='store_true', help='Print missing pkgbase names (space delimited)')
    parser.add_argument('--outdated-any', action='store_true', help='Show outdated any packages')
    parser.add_argument('--missing-any', action='store_true', help='Show missing any packages')
    parser.add_argument('--repo-mismatches', action='store_true', help='Show repository mismatches')
    parser.add_argument('--arm-newer', action='store_true', help='Show packages where AArch64 is newer')
    parser.add_argument('--arm-only', action='store_true', help='Show AArch64 only packages')
    parser.add_argument('--arm-duplicates', action='store_true', help='Show AArch64 packages in both core and extra')
    args = parser.parse_args()
    
    # Load blacklist
    blacklist_file = args.blacklist or 'blacklist.txt'
    blacklist = load_blacklist(blacklist_file) if Path(blacklist_file).exists() else []
    
    # Load packages using shared functions
    print("Loading x86_64 packages...")
    x86_urls = [
        f"{X86_64_MIRROR}/core/os/x86_64/core.db",
        f"{X86_64_MIRROR}/extra/os/x86_64/extra.db"
    ]
    x86_packages = load_packages_with_any(x86_urls, '_x86_64')
    print(f"Loaded {len(x86_packages)} x86_64 package names")
    
    print("Loading AArch64 packages...")
    arm_urls = [
        "https://arch-linux-repo.drzee.net/arch/core/os/aarch64/core.db",
        "https://arch-linux-repo.drzee.net/arch/extra/os/aarch64/extra.db"
    ]
    arm_packages = load_packages_with_any(arm_urls, '_aarch64')
    print(f"Loaded {len(arm_packages)} AArch64 package names")
    
    # Group by basename
    x86_bases = {}
    for pkg_name, pkg_data in x86_packages.items():
        basename = pkg_data['basename']
        x86_bases[basename] = pkg_data
    
    arm_bases = {}
    arm_repo_count = {}
    for pkg_name, pkg_data in arm_packages.items():
        basename = pkg_data['basename']
        arm_bases[basename] = pkg_data
        # Track which repos each basename appears in
        if basename not in arm_repo_count:
            arm_repo_count[basename] = set()
        arm_repo_count[basename].add(pkg_data['repo'])
    
    # Find packages in both core and extra (AArch64)
    for basename, repos in arm_repo_count.items():
        if len(repos) > 1:
            arm_duplicates.append(f"{basename}: present in {', '.join(sorted(repos))}")
    
    print(f"x86_64 packages: {len(x86_bases)} pkgbase")
    print(f"AArch64 packages: {len(arm_bases)} pkgbase")
    
    # Build provides lookup for x86_64
    x86_provides = {}
    for pkg_name, pkg_data in x86_packages.items():
        for provide in pkg_data.get('provides', []):
            # Strip version info from provides (e.g., "electron31=1.0" -> "electron31")
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
            x86_provides[provide_name] = pkg_name
    
    repo_mismatches = []
    arm_newer = []
    arm_only = []
    any_outdated = []
    any_missing = []
    missing_pkgbase = []
    arm_duplicates = []
    
    # Find missing pkgbase in AArch64
    for basename in x86_bases:
        if basename not in arm_bases:
            # Check if basename matches any blacklist pattern
            is_blacklisted = False
            for pattern in blacklist:
                if fnmatch.fnmatch(basename, pattern):
                    is_blacklisted = True
                    break
            if not is_blacklisted:
                missing_pkgbase.append(basename)
    
    # Check AArch64 packages
    for basename, arm_data in arm_bases.items():
        if basename in x86_bases:
            x86_data = x86_bases[basename]
            
            # Check for outdated any packages - check all individual packages for this basename
            for pkg_name, pkg_data in arm_packages.items():
                if pkg_data['basename'] == basename:
                    if pkg_data.get('arch') == 'any' or pkg_data.get('filename', '').endswith('any.pkg.tar.zst'):
                        try:
                            if version.parse(pkg_data['version']) < version.parse(x86_data['version']):
                                any_outdated.append(f"{pkg_name}: AArch64={pkg_data['version']}, x86_64={x86_data['version']}")
                        except:
                            pass
            
            # Check repo mismatch
            if arm_data['repo'] != x86_data['repo']:
                # Show individual packages and their actual repositories
                repo_mismatches.append(f"{basename}:")
                
                # Show AArch64 packages with their actual repos
                for name, data in arm_packages.items():
                    if data['basename'] == basename:
                        filename = f"{name}-{data['version']}-{data.get('arch', 'aarch64')}.pkg.tar.zst"
                        repo_mismatches.append(f"  AArch64 ({data['repo']}): {filename}")
                
                # Show x86_64 packages with their actual repos
                for name, data in x86_packages.items():
                    if data['basename'] == basename:
                        filename = f"{name}-{data['version']}-{data.get('arch', 'x86_64')}.pkg.tar.zst"
                        repo_mismatches.append(f"  x86_64 ({data['repo']}): {filename}")
                
                repo_mismatches.append("")  # Empty line for separation
            
            # Check if ARM newer
            try:
                if version.parse(arm_data['version']) > version.parse(x86_data['version']):
                    filename = arm_data.get('filename', 'unknown')
                    arm_newer.append(f"{basename}: AArch64={arm_data['version']}, x86_64={x86_data['version']} (file: {filename})")
            except:
                pass
        else:
            # Check if this package is provided by something in x86_64
            is_provided = False
            for pkg_name, pkg_data in arm_packages.items():
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
            
            if not is_provided:
                # Package only in AArch64 and not provided by x86_64
                # Find all package names for this basename
                pkg_names = [name for name, data in arm_packages.items() if data['basename'] == basename]
                if len(pkg_names) == 1 and pkg_names[0] == basename:
                    # Single package with same name as basename
                    filename = arm_data.get('filename', 'unknown')
                    arm_only.append(f"{basename}: {arm_data['version']} ({arm_data['repo']}) (file: {filename})")
                else:
                    # Multiple packages or different names
                    pkg_names_str = ', '.join(pkg_names)
                    filename = arm_data.get('filename', 'unknown')
                    arm_only.append(f"{basename} [{pkg_names_str}]: {arm_data['version']} ({arm_data['repo']}) (file: {filename})")
    
    # Check for missing 'any' packages in AArch64
    for basename, x86_data in x86_bases.items():
        if basename not in arm_bases:
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
    show_all = not any([args.outdated_any, args.missing_any, args.repo_mismatches, args.arm_newer, args.arm_only, args.arm_duplicates])
    
    if show_all or args.outdated_any:
        if any_outdated:
            print(f"\nOutdated 'any' Packages in AArch64 ({len(any_outdated)}):")
            for pkg in sorted(any_outdated):
                print(f"  {pkg}")
    
    if show_all or args.missing_any:
        if any_missing:
            print(f"\nMissing 'any' Packages in AArch64 ({len(any_missing)}):")
            for pkg in sorted(any_missing):
                print(f"  {pkg}")
    
    if show_all or args.repo_mismatches:
        if repo_mismatches:
            print(f"\nRepository Mismatches ({len(repo_mismatches)}):")
            for mismatch in sorted(repo_mismatches):
                print(f"  {mismatch}")
    
    if show_all or args.arm_newer:
        if arm_newer:
            print(f"\nAArch64 Newer Versions ({len(arm_newer)}):")
            for pkg in sorted(arm_newer):
                print(f"  {pkg}")
    
    if show_all or args.arm_duplicates:
        if arm_duplicates:
            print(f"\nAArch64 Packages in Multiple Repositories ({len(arm_duplicates)}):")
            for pkg in sorted(arm_duplicates):
                print(f"  {pkg}")
    
    if show_all or args.arm_only:
        if arm_only:
            print(f"\nAArch64 Only Packages ({len(arm_only)}):")
            for pkg in sorted(arm_only)[:10]:  # Show first 10
                print(f"  {pkg}")
            if len(arm_only) > 10:
                print(f"  ... and {len(arm_only) - 10} more")
    
    if show_all and not repo_mismatches and not arm_newer and not arm_only and not any_outdated and not any_missing and not arm_duplicates:
        print("No issues found")

if __name__ == "__main__":
    main()
