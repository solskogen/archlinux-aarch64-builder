#!/usr/bin/env python3

import argparse
from pathlib import Path
from packaging import version

from utils import load_blacklist, load_x86_64_packages, load_aarch64_packages

def main():
    parser = argparse.ArgumentParser(description='Analyze repository differences')
    parser.add_argument('--blacklist', help='Blacklist file (default: blacklist.txt)')
    args = parser.parse_args()
    
    # Load blacklist
    blacklist_file = args.blacklist or 'blacklist.txt'
    blacklist = load_blacklist(blacklist_file) if Path(blacklist_file).exists() else []
    
    # Load packages using shared functions
    print("Loading x86_64 packages...")
    x86_packages = load_x86_64_packages()
    print(f"Loaded {len(x86_packages)} x86_64 packages")
    
    print("Loading AArch64 packages...")
    arm_packages = load_aarch64_packages()
    print(f"Loaded {len(arm_packages)} AArch64 packages")
    
    print(f"Total AArch64 packages: {len(arm_packages)}")
    
    # Group by basename
    x86_bases = {}
    for pkg_name, pkg_data in x86_packages.items():
        basename = pkg_data['basename']
        x86_bases[basename] = pkg_data
    
    arm_bases = {}
    for pkg_name, pkg_data in arm_packages.items():
        basename = pkg_data['basename']
        arm_bases[basename] = pkg_data
    
    print(f"x86_64 basenames: {len(x86_bases)}")
    print(f"AArch64 basenames: {len(arm_bases)}")
    
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
    
    # Check AArch64 packages
    for basename, arm_data in arm_bases.items():
        if basename in x86_bases:
            x86_data = x86_bases[basename]
            
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
                arm_only.append(f"{basename}: {arm_data['version']} ({arm_data['repo']})")
    
    # Output
    if repo_mismatches:
        print(f"\nRepository Mismatches ({len(repo_mismatches)}):")
        for mismatch in sorted(repo_mismatches):
            print(f"  {mismatch}")
    
    if arm_newer:
        print(f"\nAArch64 Newer Versions ({len(arm_newer)}):")
        for pkg in sorted(arm_newer):
            print(f"  {pkg}")
    
    if arm_only:
        print(f"\nAArch64 Only Packages ({len(arm_only)}):")
        for pkg in sorted(arm_only)[:10]:  # Show first 10
            print(f"  {pkg}")
        if len(arm_only) > 10:
            print(f"  ... and {len(arm_only) - 10} more")
    
    if not repo_mismatches and not arm_newer and not arm_only:
        print("No issues found")

if __name__ == "__main__":
    main()
