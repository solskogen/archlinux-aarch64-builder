#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

from generate_build_list import extract_packages

def find_dependents(target_package, packages):
    """Find all packages that depend on the target package"""
    dependents = set()
    
    for pkg_name, pkg_data in packages.items():
        # Check depends and makedepends
        all_deps = pkg_data.get('depends', []) + pkg_data.get('makedepends', [])
        
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
    args = parser.parse_args()
    
    # Parse AArch64 packages
    packages = {}
    
    db_files = [
        ("core_aarch64.db", "core"),
        ("extra_aarch64.db", "extra")
    ]
    
    for db_file, repo_name in db_files:
        db_path = Path(db_file)
        if db_path.exists():
            repo_packages = extract_packages(db_path, repo_name)
            packages.update(repo_packages)
        else:
            print(f"Warning: {db_file} not found", file=sys.stderr)
    
    if not packages:
        print("No packages found", file=sys.stderr)
        sys.exit(1)
    
    # Find dependents
    dependents = find_dependents(args.package, packages)
    
    if dependents:
        print(' '.join(dependents))
    else:
        print(f"No packages depend on {args.package}", file=sys.stderr)

if __name__ == "__main__":
    main()
