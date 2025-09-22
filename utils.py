#!/usr/bin/env python3
import os
import fnmatch
from pathlib import Path

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
