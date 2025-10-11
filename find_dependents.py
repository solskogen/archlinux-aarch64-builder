#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

from utils import parse_database_file

def find_dependents(target_package, packages, check_depends=True, check_makedepends=True):
    """Find all packages that depend on the target package"""
    dependents = set()
    
    for pkg_name, pkg_data in packages.items():
        # Check depends and makedepends based on options
        all_deps = []
        if check_depends:
            all_deps.extend(pkg_data.get('depends', []))
        if check_makedepends:
            all_deps.extend(pkg_data.get('makedepends', []))
        
        for dep in all_deps:
            # Strip version constraints (>=, =, <, etc.)
            dep_name = dep.split('>=')[0].split('=')[0].split('<')[0].split('>')[0]
            
            if dep_name == target_package:
                dependents.add(pkg_data['basename'])
                break
    
    return sorted(dependents)

def main():
    parser = argparse.ArgumentParser(description='Find packages that depend on a given package')
    parser.add_argument('package', help='Package name to find dependents for')
    parser.add_argument('--depends-only', action='store_true', help='Only check runtime dependencies (depends)')
    parser.add_argument('--makedepends-only', action='store_true', help='Only check build dependencies (makedepends)')
    args = parser.parse_args()
    
    # Validate mutually exclusive options
    if args.depends_only and args.makedepends_only:
        print("Error: --depends-only and --makedepends-only are mutually exclusive", file=sys.stderr)
        sys.exit(1)
    
    # Determine what to check
    check_depends = not args.makedepends_only
    check_makedepends = not args.depends_only
    
    # Parse AArch64 packages
    packages = {}
    
    db_files = [
        ("core_aarch64.db", "core"),
        ("extra_aarch64.db", "extra")
    ]
    
    for db_file, repo_name in db_files:
        db_path = Path(db_file)
        if db_path.exists():
            repo_packages = parse_database_file(db_path)
            for pkg in repo_packages.values():
                pkg['repo'] = repo_name
            packages.update(repo_packages)
        else:
            print(f"Warning: {db_file} not found", file=sys.stderr)
    
    if not packages:
        print("No packages found", file=sys.stderr)
        sys.exit(1)
    
    # Find dependents
    dependents = find_dependents(args.package, packages, check_depends, check_makedepends)
    
    if dependents:
        print(' '.join(dependents))
    else:
        dep_type = "runtime dependencies" if args.depends_only else "build dependencies" if args.makedepends_only else "dependencies"
        print(f"No packages have {dep_type} on {args.package}", file=sys.stderr)

if __name__ == "__main__":
    main()
