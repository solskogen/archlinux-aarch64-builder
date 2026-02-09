#!/usr/bin/env python3
"""Find package dependencies in both directions."""

import argparse
import sys
from pathlib import Path

from utils import parse_database_file, get_target_architecture


def find_dependents(target_package, packages, check_depends=True, check_makedepends=True):
    """Find all packages that depend on the target package"""
    dependents = set()
    
    for pkg_name, pkg_data in packages.items():
        all_deps = []
        if check_depends:
            all_deps.extend(pkg_data.get('depends', []))
        if check_makedepends:
            all_deps.extend(pkg_data.get('makedepends', []))
        
        for dep in all_deps:
            dep_name = dep.split('>=')[0].split('=')[0].split('<')[0].split('>')[0]
            if dep_name == target_package:
                dependents.add(pkg_data['basename'])
                break
    
    return sorted(dependents)


def find_dependencies(target_package, packages, check_depends=True, check_makedepends=True):
    """Find all packages that the target package depends on"""
    pkg_data = packages.get(target_package)
    if not pkg_data:
        # Try finding by basename
        for name, data in packages.items():
            if data['basename'] == target_package:
                pkg_data = data
                break
    
    if not pkg_data:
        return []
    
    deps = set()
    if check_depends:
        for dep in pkg_data.get('depends', []):
            deps.add(dep.split('>=')[0].split('=')[0].split('<')[0].split('>')[0])
    if check_makedepends:
        for dep in pkg_data.get('makedepends', []):
            deps.add(dep.split('>=')[0].split('=')[0].split('<')[0].split('>')[0])
    
    return sorted(deps)


def load_packages():
    """Load packages from database files"""
    target_arch = get_target_architecture()
    packages = {}
    
    db_files = [
        (f"core_{target_arch}.db", "core"),
        (f"extra_{target_arch}.db", "extra")
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
    
    return packages


def main():
    parser = argparse.ArgumentParser(
        description='Query package dependency relationships',
        epilog='Examples:\n'
               '  %(prog)s gcc          # What depends on gcc?\n'
               '  %(prog)s -f gcc       # What does gcc depend on?',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('package', help='Package name to query')
    parser.add_argument('-f', '--forward', action='store_true', 
                        help='Show dependencies OF this package (default: show dependents)')
    parser.add_argument('--depends-only', action='store_true', help='Runtime dependencies only')
    parser.add_argument('--makedepends-only', action='store_true', help='Build dependencies only')
    args = parser.parse_args()
    
    if args.depends_only and args.makedepends_only:
        print("Error: --depends-only and --makedepends-only are mutually exclusive", file=sys.stderr)
        sys.exit(1)
    
    check_depends = not args.makedepends_only
    check_makedepends = not args.depends_only
    
    packages = load_packages()
    if not packages:
        print("No packages found", file=sys.stderr)
        sys.exit(1)
    
    if args.forward:
        results = find_dependencies(args.package, packages, check_depends, check_makedepends)
        direction = "Dependencies of"
    else:
        results = find_dependents(args.package, packages, check_depends, check_makedepends)
        direction = "Packages depending on"
    
    if results:
        print(' '.join(results))
    else:
        print(f"No results for {direction.lower()} {args.package}", file=sys.stderr)


if __name__ == "__main__":
    main()
