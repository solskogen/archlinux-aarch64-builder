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


def find_rebuild_order(target_packages, packages, max_depth=None):
    """Find all reverse dependencies recursively and return in topological build order as pkgbase names."""
    # Build provides map: provide_name -> pkg_name
    provides_map = {}
    for name, data in packages.items():
        provides_map[name] = name
        for p in data.get('provides', []):
            provides_map[p.split('=')[0].split('>')[0].split('<')[0]] = name

    def extract_dep(d):
        return d.split('>=')[0].split('=')[0].split('<')[0].split('>')[0]

    # Find all reverse deps recursively
    to_rebuild = set()
    queue = [(pkg, 0) for pkg in target_packages]
    for pkg, depth in queue:
        if max_depth is not None and depth >= max_depth:
            continue
        # Find everything that depends on pkg
        for name, data in packages.items():
            basename = data['basename']
            if basename in to_rebuild:
                continue
            all_deps = [extract_dep(d) for d in data.get('depends', []) + data.get('makedepends', [])]
            # Resolve through provides
            for dep in all_deps:
                resolved = provides_map.get(dep, dep)
                resolved_base = packages[resolved]['basename'] if resolved in packages else resolved
                if resolved == pkg or resolved_base == pkg or dep == pkg:
                    to_rebuild.add(basename)
                    queue.append((basename, depth + 1))
                    break

    # Topological sort by dependencies within the rebuild set
    # Build dep graph among rebuild set
    from collections import defaultdict, deque
    graph = defaultdict(set)  # pkg -> set of pkgs that depend on it
    in_degree = {b: 0 for b in to_rebuild}

    for name, data in packages.items():
        basename = data['basename']
        if basename not in to_rebuild:
            continue
        all_deps = [extract_dep(d) for d in data.get('depends', []) + data.get('makedepends', [])]
        seen = set()
        for dep in all_deps:
            resolved = provides_map.get(dep, dep)
            dep_base = packages[resolved]['basename'] if resolved in packages else resolved
            if dep_base in to_rebuild and dep_base != basename and dep_base not in seen:
                seen.add(dep_base)
                graph[dep_base].add(basename)
                in_degree[basename] += 1

    # Kahn's algorithm
    result = []
    queue = deque(b for b in to_rebuild if in_degree[b] == 0)
    while queue:
        pkg = queue.popleft()
        result.append(pkg)
        for dep in sorted(graph[pkg]):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Append any remaining (cycles)
    remaining = [b for b in to_rebuild if b not in result]
    result.extend(sorted(remaining))

    return result


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
               '  %(prog)s -f gcc       # What does gcc depend on?\n'
               '  %(prog)s -r gcc       # Rebuild order for gcc and all its dependents',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('package', nargs='+', help='Package name(s) to query')
    parser.add_argument('-f', '--forward', action='store_true', 
                        help='Show dependencies OF this package (default: show dependents)')
    parser.add_argument('-r', '--rebuild-order', action='store_true',
                        help='Show all reverse deps in topological build order (pkgbase names)')
    parser.add_argument('-d', '--depth', type=int, default=None,
                        help='Max dependency depth for -r (default: unlimited)')
    parser.add_argument('--depends-only', action='store_true', help='Runtime dependencies only')
    parser.add_argument('--makedepends-only', action='store_true', help='Build dependencies only')
    parser.add_argument('--ignore-self', action='store_true',
                        help='Exclude the queried package(s) from the output, even if they appear (e.g. a split package depending on its own pkgbase)')
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

    # Build set of names to ignore: requested names plus their pkgbase equivalents.
    ignore_set = set()
    if args.ignore_self:
        for name in args.package:
            ignore_set.add(name)
            # Also add the pkgbase if the user gave a package name that maps to a different basename
            if name in packages:
                ignore_set.add(packages[name]['basename'])
            # And any package that belongs to a basename matching the requested name
            for pname, pdata in packages.items():
                if pdata['basename'] == name:
                    ignore_set.add(pname)

    if args.rebuild_order:
        results = find_rebuild_order(args.package, packages, max_depth=args.depth)
        if args.ignore_self:
            results = [r for r in results if r not in ignore_set]
        if results:
            print(' '.join(results))
        else:
            print(f"No reverse dependencies found for {' '.join(args.package)}", file=sys.stderr)
    elif args.forward:
        results = find_dependencies(args.package[0], packages, check_depends, check_makedepends)
        if args.ignore_self:
            results = [r for r in results if r not in ignore_set]
        if results:
            print(' '.join(results))
        else:
            print(f"No dependencies found for {args.package[0]}", file=sys.stderr)
    else:
        results = find_dependents(args.package[0], packages, check_depends, check_makedepends)
        if args.ignore_self:
            results = [r for r in results if r not in ignore_set]
        if results:
            print(' '.join(results))
        else:
            print(f"No dependents found for {args.package[0]}", file=sys.stderr)


if __name__ == "__main__":
    main()
