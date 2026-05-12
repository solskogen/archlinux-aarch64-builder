#!/usr/bin/env python3
"""Analyze differences between x86_64 and target architecture repositories."""

import argparse
import fnmatch
from pathlib import Path
from packaging import version

from utils import (
    load_blacklist, get_target_architecture, is_version_newer,
    load_all_packages_parallel, ArchVersionComparator
)


def build_provides_map(packages):
    """Build provides mapping: provide_name -> pkg_name"""
    provides = {}
    for pkg_name, pkg_data in packages.items():
        for provide in pkg_data.get('provides', []):
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
            provides[provide_name] = pkg_name
    return provides


def group_by_basename(packages):
    """Group packages by basename, return (bases, by_basename, repo_count)"""
    bases = {}
    by_basename = {}
    repo_count = {}
    
    for pkg_name, pkg_data in packages.items():
        basename = pkg_data['basename']
        bases[basename] = pkg_data
        by_basename.setdefault(basename, []).append(pkg_name)
        repo_count.setdefault(basename, set()).add(pkg_data['repo'])
    
    return bases, by_basename, repo_count


def is_blacklisted(basename, pkg_data, blacklist):
    """Check if package or its dependencies are blacklisted"""
    for pattern in blacklist:
        if fnmatch.fnmatch(basename, pattern):
            return True
    
    all_deps = pkg_data.get('depends', []) + pkg_data.get('makedepends', [])
    for dep in all_deps:
        dep_name = dep.split('=')[0].split('>')[0].split('<')[0]
        for pattern in blacklist:
            if fnmatch.fnmatch(dep_name, pattern):
                return True
    return False


def find_package_name_mismatches(x86_bases, x86_by_basename, target_by_basename, 
                                  target_packages, x86_provides, target_provides, target_arch):
    """Find packages where split package names differ between architectures"""
    mismatches = []
    
    for basename in x86_bases:
        if basename not in target_by_basename:
            continue
            
        x86_pkg_names = set(x86_by_basename[basename])
        target_pkg_names = set(target_by_basename[basename])
        
        x86_only = x86_pkg_names - target_pkg_names
        target_only = target_pkg_names - x86_pkg_names
        
        if x86_only or target_only:
            parts = []
            if x86_only:
                x86_details = []
                for p in sorted(x86_only):
                    # Check if provided by a target package
                    if p in target_provides:
                        x86_details.append(f"{p} (provided by {target_provides[p]})")
                    else:
                        x86_details.append(p)
                parts.append(f"x86_64 has {', '.join(x86_details)}")
            if target_only:
                target_with_files = []
                for pkg_name in sorted(target_only):
                    if pkg_name in target_packages:
                        pkg = target_packages[pkg_name]
                        filename = pkg.get('filename', f"{pkg_name}-{pkg.get('version', 'unknown')}-{target_arch}.pkg.tar.zst")
                        target_with_files.append(f"{pkg_name} ({filename})")
                    else:
                        target_with_files.append(pkg_name)
                parts.append(f"{target_arch} has {', '.join(target_with_files)}")
            mismatches.append(f"{basename}: {', '.join(parts)}")
    
    return mismatches


def find_outdated_any_packages(target_by_basename, target_packages, x86_bases, target_arch, x86_packages):
    """Find ARCH=any packages that are outdated compared to x86_64"""
    outdated = []
    
    for basename, pkg_names in target_by_basename.items():
        if basename not in x86_bases:
            continue
        x86_arch = x86_bases[basename].get('arch', 'unknown')
        
        for pkg_name in pkg_names:
            pkg = target_packages[pkg_name]
            target_pkg_arch = pkg.get('arch') or ('any' if pkg.get('filename', '').endswith('any.pkg.tar.zst') else 'unknown')
            
            # Compare against the actual x86 package of the same name, not basename —
            # split packages (e.g. firefox-i18n-*) can lag behind their siblings during
            # a partial rebuild, and using x86_bases[basename]['version'] gives an
            # arbitrary sibling's version.
            x86_pkg = x86_packages.get(pkg_name)
            if not x86_pkg:
                continue  # Not present under the same name on x86; skip
            x86_version = x86_pkg['version']
            
            # Only report if target package is 'any' AND x86_64 package is also 'any'
            if target_pkg_arch == 'any' and x86_arch == 'any':
                try:
                    if version.parse(pkg['version']) < version.parse(x86_version):
                        outdated.append(f"{pkg_name}: {target_arch}={pkg['version']}, x86_64={x86_version}")
                except Exception:
                    pass
            # Report architecture changes
            elif target_pkg_arch == 'any' and x86_arch != 'any':
                outdated.append(f"{pkg_name}: {target_arch}={pkg['version']} (any), x86_64={x86_version} (ARCH CHANGED to {x86_arch})")
            elif target_pkg_arch != 'any' and x86_arch == 'any':
                outdated.append(f"{pkg_name}: {target_arch}={pkg['version']} ({target_pkg_arch}), x86_64={x86_version} (ARCH CHANGED to any)")
    
    return outdated


def find_missing_any_packages(x86_bases, x86_packages, target_bases):
    """Find ARCH=any packages missing from target architecture"""
    missing = []
    
    for basename in x86_bases:
        if basename in target_bases:
            continue
        for pkg_name, pkg in x86_packages.items():
            if pkg['basename'] == basename:
                if pkg.get('arch') == 'any' or pkg.get('filename', '').endswith('any.pkg.tar.zst'):
                    missing.append(f"{pkg_name}: x86_64={pkg['version']} ({pkg['repo']})")
                    break
    
    return missing


def find_repo_issues(target_bases, x86_bases, target_repo_count, target_arch):
    """Find repository mismatches and duplicates"""
    issues = []
    
    # Packages in multiple repos on target
    for basename, repos in target_repo_count.items():
        if len(repos) > 1:
            issues.append(f"{basename}: present in {', '.join(sorted(repos))} on {target_arch}")
    
    # Check for cross-repo duplicates by parsing raw DB files
    from utils import parse_database_file, get_target_architecture
    _target_arch = get_target_architecture()
    pkg_repos = {}  # pkg_name -> set of repos
    for db_file, repo_name in [(f'core_{_target_arch}.db', 'core'), (f'extra_{_target_arch}.db', 'extra'), (f'forge_{_target_arch}.db', 'forge')]:
        for name in parse_database_file(db_file, include_any=True):
            pkg_repos.setdefault(name, set()).add(repo_name)
    for name, repos in sorted(pkg_repos.items()):
        if len(repos) > 1:
            issues.append(f"{name}: present in {', '.join(sorted(repos))} on {target_arch}")
    
    # Repo mismatches between architectures
    for basename, target_data in target_bases.items():
        if basename in x86_bases:
            x86_repo = x86_bases[basename]['repo']
            if target_data['repo'] != x86_repo:
                issues.append(f"{basename}: {target_arch} in {target_data['repo']}, x86_64 in {x86_repo}")
    
    return issues


def find_target_newer(target_bases, x86_bases, target_arch):
    """Find packages where target architecture is newer than x86_64"""
    newer = []
    
    for basename, target_data in target_bases.items():
        if basename in x86_bases:
            x86_version = x86_bases[basename]['version']
            try:
                if is_version_newer(x86_version, target_data['version']):
                    newer.append(f"{basename}: {target_arch} {target_data['version']} > x86_64 {x86_version}")
            except Exception:
                pass
    
    return newer


def get_bin_package_version_info(basename, target_data, x86_packages):
    """Get version comparison info for -bin packages"""
    if not basename.endswith('-bin'):
        return ""
    
    counterpart = basename[:-4]
    x86_version = None
    x86_counterpart = None
    
    if counterpart in x86_packages:
        x86_counterpart = x86_packages[counterpart]['basename']
        x86_version = x86_packages[counterpart]['version']
    else:
        for provide in target_data.get('provides', []):
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0].strip()
            if provide_name in x86_packages:
                x86_counterpart = x86_packages[provide_name]['basename']
                x86_version = x86_packages[provide_name]['version']
                break
    
    if not x86_version:
        return ""
    
    # Compare pkgver only (ignore pkgrel)
    target_pkgver = target_data['version'].rsplit('-', 1)[0]
    x86_pkgver = x86_version.rsplit('-', 1)[0]
    
    if target_pkgver == x86_pkgver:
        return f" \033[32m[matches x86_64 {x86_counterpart}]\033[0m"
    
    try:
        if version.parse(target_pkgver) > version.parse(x86_pkgver):
            return f" \033[36m[NEWER than x86_64 {x86_counterpart}: {x86_version}]\033[0m"
        return f" \033[31m[OUTDATED - x86_64 {x86_counterpart}: {x86_version}]\033[0m"
    except Exception:
        return f" [x86_64 {x86_counterpart}: {x86_version}]"


def find_target_only(target_bases, target_packages, target_by_basename, x86_bases, 
                     x86_packages, x86_provides, target_arch):
    """Find packages that only exist on target architecture"""
    only = []
    
    for basename, target_data in target_bases.items():
        if basename in x86_bases:
            continue
        
        pkg_names = target_by_basename[basename]
        arch = target_data.get('arch', target_arch)
        if isinstance(arch, set):
            arch = list(arch)[0] if arch else target_arch
        
        if len(pkg_names) == 1 and pkg_names[0] == basename:
            filename = f"{basename}-{target_data['version']}-{arch}.pkg.tar.zst"
            # For packages with incorrect basename grouping, use the actual package data
            actual_pkg_data = target_packages.get(basename, target_data)
            version_info = get_bin_package_version_info(basename, actual_pkg_data, x86_packages)
            line = f"{basename}: {target_data['version']} ({target_data['repo']}) (file: {filename}){version_info}"
            if target_data['repo'] in ['core', 'extra']:
                line = f"\033[31m{line}\033[0m"
            only.append(line)
        else:
            for pkg_name in pkg_names:
                filename = f"{pkg_name}-{target_data['version']}-{arch}.pkg.tar.zst"
                # Use actual package data for version info
                actual_pkg_data = target_packages.get(pkg_name, target_data)
                version_info = get_bin_package_version_info(pkg_name, actual_pkg_data, x86_packages)
                line = f"{pkg_name}: {target_data['version']} ({target_data['repo']}) (file: {filename}){version_info}"
                if target_data['repo'] in ['core', 'extra']:
                    line = f"\033[31m{line}\033[0m"
                only.append(line)
    
    return only


def find_orphaned_split_packages(x86_packages, target_packages, x86_bases):
    """Find target packages whose pkgbase exists in x86 but the package name was removed"""
    orphans = []
    for name, pkg in target_packages.items():
        basename = pkg['basename']
        if basename in x86_bases and name not in x86_packages:
            filename = pkg.get('filename', f"{name}-{pkg['version']}-{pkg.get('arch', 'aarch64')}.pkg.tar.zst")
            orphans.append(f"{name}: {pkg['version']} (pkgbase={basename}, repo={pkg['repo']}, file={filename})")
    return orphans


# --- Dependency health checks ----------------------------------------------

def _parse_dep(dep_str):
    """
    Parse a pacman dep string into (name, op, version).
    
    Examples:
        'glibc'             -> ('glibc', None, None)
        'glibc>=2.41'       -> ('glibc', '>=', '2.41')
        'libfoo.so=5-64'    -> ('libfoo.so', '=', '5-64')
    """
    import re
    m = re.match(r'^([^<>=]+)(<=|>=|<|>|=)(.+)$', dep_str.strip())
    if m:
        return m.group(1).strip(), m.group(2), m.group(3).strip()
    return dep_str.strip(), None, None


def _constraint_satisfied(op, required, actual):
    """Check if `actual` version satisfies `op required`."""
    if op is None or actual is None:
        return True
    cmp = ArchVersionComparator.compare(actual, required)
    return {'=': cmp == 0, '<': cmp < 0, '<=': cmp <= 0,
            '>': cmp > 0, '>=': cmp >= 0}.get(op, True)


def _build_target_provides_index(target_packages):
    """
    Build two lookups:
      provides_name -> list of (provider_pkg, provided_version_or_None)
      
    Includes:
      - Each package's own name (maps to its own version)
      - Each package's basename
      - Each PROVIDES entry (with version if present)
    """
    idx = {}
    for name, pkg in target_packages.items():
        ver = pkg.get('version')
        idx.setdefault(name, []).append((name, ver))
        basename = pkg.get('basename', name)
        if basename != name:
            idx.setdefault(basename, []).append((name, ver))
        for provide in pkg.get('provides', []):
            p_name, _, p_ver = _parse_dep(provide)
            idx.setdefault(p_name, []).append((name, p_ver or ver))
    return idx


def find_broken_and_outdated_deps(target_packages, target_arch):
    """
    Scan every target package's runtime depends. Report:
      - broken: dep name not resolvable in target at all
      - unsatisfied: dep resolved but version constraint not met (covers SONAME drift)
    
    Only checks 'depends' (runtime) — makedepends don't affect shipped packages.
    """
    broken = []
    unsatisfied = []
    provides_idx = _build_target_provides_index(target_packages)
    
    for pkg_name, pkg in target_packages.items():
        for dep_str in pkg.get('depends', []):
            dep_name, op, req_ver = _parse_dep(dep_str)
            providers = provides_idx.get(dep_name)
            
            if not providers:
                broken.append(f"{pkg_name} depends on '{dep_str}' — not available on {target_arch}")
                continue
            
            # Check if any provider satisfies the version constraint
            if op is None:
                continue  # No constraint, already satisfied by existence
            
            satisfied = False
            details = []
            for provider, actual_ver in providers:
                if _constraint_satisfied(op, req_ver, actual_ver):
                    satisfied = True
                    break
                details.append(f"{provider} provides {dep_name}={actual_ver or '(unversioned)'}")
            
            if not satisfied:
                # Classify: SONAME drift (e.g. libfoo.so=5-64) vs normal version constraint
                is_soname = '.so' in dep_name and op == '='
                label = 'SONAME drift' if is_soname else 'unsatisfied'
                unsatisfied.append(
                    f"[{label}] {pkg_name} needs '{dep_str}' — target has: {'; '.join(details)}"
                )
    
    return broken, unsatisfied


def find_blocked_by_blacklist(missing_pkgbase, x86_packages, x86_bases,
                               target_provides, blacklist):
    """
    For each missing pkgbase, walk its dependency chain. If any transitive dep
    basename is blacklisted, the package is blocked — report the chain.
    
    Only considers runtime depends + makedepends (what actually must be built).
    """
    if not blacklist:
        return []
    
    # Build reverse: x86 pkg_name -> basename (with provides)
    x86_name_to_base = {}
    for name, pkg in x86_packages.items():
        x86_name_to_base[name] = pkg['basename']
        for provide in pkg.get('provides', []):
            p_name, _, _ = _parse_dep(provide)
            x86_name_to_base.setdefault(p_name, pkg['basename'])
    
    def _matches_blacklist(basename):
        for pat in blacklist:
            if fnmatch.fnmatch(basename, pat):
                return pat
        return None
    
    blocked = []
    for basename in missing_pkgbase:
        if basename not in x86_bases:
            continue
        # BFS through deps
        visited = {basename}
        queue = [(basename, [basename])]
        blockers = []
        while queue:
            current, path = queue.pop(0)
            if current not in x86_bases:
                continue
            pkg = x86_bases[current]
            for dep in pkg.get('depends', []) + pkg.get('makedepends', []):
                dep_name, _, _ = _parse_dep(dep)
                dep_base = x86_name_to_base.get(dep_name, dep_name)
                if dep_base in visited:
                    continue
                visited.add(dep_base)
                # Skip if already provided on target (not blocking us)
                if dep_name in target_provides or dep_base in target_provides:
                    continue
                pat = _matches_blacklist(dep_base)
                if pat:
                    chain = ' -> '.join(path + [dep_base])
                    blockers.append(f"{chain} [matches '{pat}']")
                    continue  # Don't recurse through blacklisted nodes
                if dep_base in x86_bases:
                    queue.append((dep_base, path + [dep_base]))
        if blockers:
            blocked.append(f"{basename}: blocked by {blockers[0]}"
                           + (f" (+{len(blockers)-1} more paths)" if len(blockers) > 1 else ""))
    
    return blocked


# ---------------------------------------------------------------------------


def print_section(title, items, show_empty=True):
    """Print a section with title and items"""
    if items:
        print(f"\n{title} ({len(items)}):")
        for item in sorted(items):
            print(f"  {item}")
    elif show_empty:
        print(f"\n{title}: None found")


def main():
    target_arch = get_target_architecture()
    
    parser = argparse.ArgumentParser(
        description=f'Analyze differences between x86_64 and {target_arch} repositories'
    )
    parser.add_argument('--blacklist', help='Blacklist file (default: blacklist.txt)')
    parser.add_argument('--no-blacklist', action='store_true',
                        help='Ignore blacklist (use with --missing-pkgbase)')
    parser.add_argument('--use-existing-db', action='store_true', 
                        help='Use existing database files instead of downloading')
    parser.add_argument('--missing-pkgbase', action='store_true', 
                        help='Print missing pkgbase names (space delimited)')
    parser.add_argument('--outdated-any', action='store_true', help='Show outdated any packages')
    parser.add_argument('--missing-any', action='store_true', help='Show missing any packages')
    parser.add_argument('--repo-issues', action='store_true', 
                        help='Show repository inconsistencies and duplicates')
    parser.add_argument('--target-newer', action='store_true', 
                        help=f'Show packages where {target_arch} is newer')
    parser.add_argument('--target-only', action='store_true', 
                        help=f'Show {target_arch} only packages')
    parser.add_argument('--orphaned', action='store_true',
                        help='Show orphaned split packages (removed upstream but still in target)')
    parser.add_argument('--broken-deps', action='store_true',
                        help='Show target packages with unresolvable dependencies')
    parser.add_argument('--unsatisfied-deps', action='store_true',
                        help='Show target packages with unsatisfied version constraints (includes SONAME drift)')
    parser.add_argument('--blocked-by-blacklist', action='store_true',
                        help='Show missing packages blocked by blacklisted dependencies')
    parser.add_argument('--target-only-files', action='store_true',
                        help=f'Print filenames of {target_arch}-only packages in core/extra')
    # Legacy aliases
    for alias, dest in [('--repo-mismatches', 'repo_issues'), ('--target-duplicates', 'repo_issues'),
                        ('--arm-newer', 'target_newer'), ('--arm-only', 'target_only'),
                        ('--arm-duplicates', 'repo_issues')]:
        parser.add_argument(alias, action='store_true', dest=dest, help=argparse.SUPPRESS)
    
    args = parser.parse_args()
    
    # Load data
    if args.no_blacklist:
        blacklist = []
    else:
        blacklist_file = args.blacklist or 'blacklist.txt'
        blacklist = load_blacklist(blacklist_file) if Path(blacklist_file).exists() else []
    
    print("Loading packages...")
    x86_packages, target_packages = load_all_packages_parallel(
        download=not args.use_existing_db, include_any=True
    )
    
    print("Processing packages...")
    x86_bases, x86_by_basename, _ = group_by_basename(x86_packages)
    target_bases, target_by_basename, target_repo_count = group_by_basename(target_packages)
    x86_provides = build_provides_map(x86_packages)
    target_provides = build_provides_map(target_packages)
    
    # Find missing pkgbase (not blacklisted, exclude ARCH=any)
    missing_pkgbase = [
        basename for basename in x86_bases
        if (basename not in target_bases and 
            not is_blacklisted(basename, x86_bases[basename], blacklist) and
            x86_bases[basename].get('arch') != 'any')
    ]
    
    if args.missing_pkgbase:
        # Split into truly missing vs provided by other packages
        truly_missing = []
        provided_by_other = []
        for basename in sorted(missing_pkgbase):
            if basename in target_provides:
                provided_by_other.append(basename)
            else:
                truly_missing.append(basename)
        
        print(f"Missing pkgbase (not available on {target_arch}): {len(truly_missing)}")
        print(' '.join(truly_missing))
        if provided_by_other:
            print(f"\nMissing pkgbase, but provided by other packages: {len(provided_by_other)}")
            for basename in provided_by_other:
                provider = target_provides[basename]
                print(f"  {basename} -> {provider}")
        return
    
    if args.target_only_files:
        for name, pkg in sorted(target_packages.items()):
            if pkg['basename'] in x86_bases:
                continue
            if pkg['repo'] not in ('core', 'extra'):
                continue
            print(pkg.get('filename', f"{name}-{pkg['version']}-{pkg.get('arch', target_arch)}.pkg.tar.zst"))
        return
    
    # Determine what to show
    show_all = not any([args.outdated_any, args.missing_any, args.repo_issues, 
                        args.target_newer, args.target_only, args.orphaned,
                        args.broken_deps, args.unsatisfied_deps, args.blocked_by_blacklist])
    
    # Collect results
    results = {
        'mismatches': find_package_name_mismatches(
            x86_bases, x86_by_basename, target_by_basename, 
            target_packages, x86_provides, target_provides, target_arch
        ) if show_all else [],
        'outdated_any': find_outdated_any_packages(
            target_by_basename, target_packages, x86_bases, target_arch, x86_packages
        ) if show_all or args.outdated_any else [],
        'missing_any': find_missing_any_packages(
            x86_bases, x86_packages, target_bases
        ) if show_all or args.missing_any else [],
        'repo_issues': find_repo_issues(
            target_bases, x86_bases, target_repo_count, target_arch
        ) if show_all or args.repo_issues else [],
        'target_newer': find_target_newer(
            target_bases, x86_bases, target_arch
        ) if show_all or args.target_newer else [],
        'target_only': find_target_only(
            target_bases, target_packages, target_by_basename, x86_bases,
            x86_packages, x86_provides, target_arch
        ) if show_all or args.target_only else [],
    }
    
    # Output
    if show_all and results['mismatches']:
        print_section("Package Name Mismatches", results['mismatches'], show_empty=False)
    
    if show_all or args.outdated_any:
        print_section("Outdated 'any' Packages in AArch64", results['outdated_any'])
    
    if show_all or args.missing_any:
        print_section("Missing 'any' Packages in AArch64", results['missing_any'],
                      show_empty=not show_all)
    
    if show_all or args.repo_issues:
        print_section("Repository Issues", results['repo_issues'],
                      show_empty=not show_all)
    
    if show_all or args.target_only:
        if results['target_only']:
            print(f"\n{target_arch} Only Packages ({len(results['target_only'])}):")
            # Sort with -bin packages at the end
            for pkg in sorted(results['target_only'], key=lambda p: (p.split(':')[0].endswith('-bin'), p)):
                print(f"  {pkg}")
        else:
            print(f"\n{target_arch} Only Packages: None found")
    
    if show_all or args.target_newer:
        print_section(f"{target_arch} Newer Versions", results['target_newer'])
    
    if show_all or args.orphaned:
        orphans = find_orphaned_split_packages(x86_packages, target_packages, x86_bases)
        print_section("Orphaned Split Packages (removed upstream)", orphans)
    
    if show_all or args.broken_deps or args.unsatisfied_deps:
        broken, unsatisfied = find_broken_and_outdated_deps(target_packages, target_arch)
        if show_all or args.broken_deps:
            print_section("Broken Dependencies (dep not available on target)", broken)
        if show_all or args.unsatisfied_deps:
            print_section("Unsatisfied Dependencies (includes SONAME drift)", unsatisfied)
    
    if show_all or args.blocked_by_blacklist:
        blocked = find_blocked_by_blacklist(
            missing_pkgbase, x86_packages, x86_bases, target_provides, blacklist
        )
        print_section("Missing Packages Blocked by Blacklisted Deps", blocked)
    
    if show_all and not any(results.values()):
        print("No issues found")


if __name__ == "__main__":
    main()
