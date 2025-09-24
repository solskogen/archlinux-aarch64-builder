#!/usr/bin/env python3
import os
import fnmatch
import subprocess
from pathlib import Path

def load_database_packages(urls, arch_suffix, download=True):
    """
    Download and parse database files for given URLs
    
    Args:
        urls: List of database URLs to download
        arch_suffix: Suffix for local filename (e.g., '_x86_64', '_aarch64')
        download: Whether to download files (False = use existing files)
    
    Returns:
        dict: Package name -> package data mapping
    """
    from generate_build_list import parse_database_file
    
    packages = {}
    
    for url in urls:
        try:
            db_filename = url.split('/')[-1].replace('.db', f'{arch_suffix}.db')
            
            if download:
                print(f"Downloading {db_filename}...")
                subprocess.run(["wget", "-q", "-O", db_filename, url], check=True)
            
            repo_name = url.split('/')[-4]  # Extract 'core' or 'extra' from URL
            print(f"Parsing {db_filename}...")
            repo_packages = parse_database_file(db_filename)
            
            for name, pkg in repo_packages.items():
                pkg['repo'] = repo_name
                packages[name] = pkg
                
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to download {url}: {e}")
        except Exception as e:
            print(f"Warning: Failed to parse {db_filename}: {e}")
    
    return packages

def load_x86_64_packages(download=True, repos=None):
    """Load x86_64 packages from official repositories"""
    urls = [
        "https://geo.mirror.pkgbuild.com/core/os/x86_64/core.db",
        "https://geo.mirror.pkgbuild.com/extra/os/x86_64/extra.db"
    ]
    
    # Filter to specific repos if requested
    if repos:
        if isinstance(repos, str):
            repos = [repos]
        filtered_urls = []
        for repo in repos:
            filtered_urls.extend([url for url in urls if f'/{repo}/' in url])
        urls = filtered_urls
    
    if not download:
        print("Using existing x86_64 databases...")
    else:
        print("Downloading x86_64 databases...")
    
    return load_database_packages(urls, '_x86_64', download)

def load_aarch64_packages(download=True, urls=None):
    """Load AArch64 packages from configured repositories"""
    if urls is None:
        urls = [
            "https://arch-linux-repo.drzee.net/arch/core/os/aarch64/core.db",
            "https://arch-linux-repo.drzee.net/arch/extra/os/aarch64/extra.db"
        ]
    
    if not download:
        print("Using existing AArch64 databases...")
    else:
        print("Downloading AArch64 databases...")
    
    return load_database_packages(urls, '_aarch64', download)

def load_blacklist(blacklist_file):
    """Load blacklisted packages with wildcard support"""
    if not blacklist_file or not Path(blacklist_file).exists():
        return []
    blacklist = []
    with open(blacklist_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                blacklist.append(line)
    return blacklist

def filter_blacklisted_packages(packages, blacklist):
    """Filter packages using blacklist with wildcard matching"""
    if not blacklist:
        return packages, 0
    
    filtered_packages = []
    for pkg in packages:
        is_blacklisted = False
        for pattern in blacklist:
            if fnmatch.fnmatch(pkg['name'], pattern) or fnmatch.fnmatch(pkg.get('basename', pkg['name']), pattern):
                is_blacklisted = True
                break
        if not is_blacklisted:
            filtered_packages.append(pkg)
    
    return filtered_packages, len(packages) - len(filtered_packages)
