#!/usr/bin/env python3
"""Analyze differences between x86_64 and target architecture repositories."""

import argparse
import fnmatch
from pathlib import Path
from packaging import version

from utils import (
    load_blacklist, get_target_architecture, is_version_newer,
    load_all_packages_parallel
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
        
        x86_only = {p for p in x86_pkg_names - target_pkg_names if p not in target_provides}
        target_only = {p for p in target_pkg_names - x86_pkg_names if p not in x86_provides}
        
        if x86_only or target_only:
            parts = []
            if x86_only:
                parts.append(f"x86_64 has {', '.join(sorted(x86_only))}")
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


def find_outdated_any_packages(target_by_basename, target_packages, x86_bases, target_arch):
    """Find ARCH=any packages that are outdated compared to x86_64"""
    outdated = []
    
    for basename, pkg_names in target_by_basename.items():
        if basename not in x86_bases:
            continue
        x86_version = x86_bases[basename]['version']
        
        for pkg_name in pkg_names:
            pkg = target_packages[pkg_name]
            if pkg.get('arch') == 'any' or pkg.get('filename', '').endswith('any.pkg.tar.zst'):
                try:
                    if version.parse(pkg['version']) < version.parse(x86_version):
                        outdated.append(f"{pkg_name}: {target_arch}={pkg['version']}, x86_64={x86_version}")
                except Exception:
                    pass
    
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
        x86_counterpart = counterpart
        x86_version = x86_packages[counterpart]['version']
    else:
        for provide in target_data.get('provides', []):
            provide_name = provide.split('=')[0].split('<')[0].split('>')[0]
            if provide_name in x86_packages:
                x86_counterpart = provide_name
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
            version_info = get_bin_package_version_info(basename, target_data, x86_packages)
            line = f"{basename}: {target_data['version']} ({target_data['repo']}) (file: {filename}){version_info}"
            if target_data['repo'] in ['core', 'extra']:
                line = f"\033[31m{line}\033[0m"
            only.append(line)
        else:
            for pkg_name in pkg_names:
                filename = f"{pkg_name}-{target_data['version']}-{arch}.pkg.tar.zst"
                only.append(f"{pkg_name}: {target_data['version']} ({target_data['repo']}) (file: {filename})")
    
    return only


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
    
    # Find missing pkgbase (not blacklisted)
    missing_pkgbase = [
        basename for basename in x86_bases
        if basename not in target_bases and not is_blacklisted(basename, x86_bases[basename], blacklist)
    ]
    
    if args.missing_pkgbase:
        print(' '.join(sorted(missing_pkgbase)))
        return
    
    # Determine what to show
    show_all = not any([args.outdated_any, args.missing_any, args.repo_issues, 
                        args.target_newer, args.target_only])
    
    # Collect results
    results = {
        'mismatches': find_package_name_mismatches(
            x86_bases, x86_by_basename, target_by_basename, 
            target_packages, x86_provides, target_provides, target_arch
        ) if show_all else [],
        'outdated_any': find_outdated_any_packages(
            target_by_basename, target_packages, x86_bases, target_arch
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
        print_section("Missing 'any' Packages in AArch64", results['missing_any'])
    
    if show_all or args.repo_issues:
        print_section("Repository Issues", results['repo_issues'])
    
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
    
    if show_all and not any(results.values()):
        print("No issues found")


if __name__ == "__main__":
    main()
