#!/usr/bin/env python3

import json
import sys
import os
import subprocess
import signal
import argparse
from pathlib import Path
from build_utils import BuildUtils
from utils import load_blacklist, filter_blacklisted_packages

class PackageBuilder:
    def __init__(self, dry_run=False, chroot_path=None, cache_dir=None, no_cache=False, no_upload=False):
        self.build_utils = BuildUtils(dry_run)
        self.dry_run = dry_run
        self.no_upload = no_upload
        self.no_cache = no_cache
        self.chroot_path = Path(chroot_path) if chroot_path else Path("/var/tmp/builder")
        self.cache_dir = Path(cache_dir) if cache_dir else Path("/var/tmp/pacman-cache")
        self.logs_dir = Path("logs")
        self.temp_copies = []
        
        # Set up signal handlers for cleanup
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Clean up temporary chroot copies on interruption"""
        print(f"\nReceived signal {signum}, cleaning up...")
        self._cleanup_temp_copies()
        sys.exit(1)
    
    def _cleanup_temp_copies(self):
        """Clean up any temporary chroot copies"""
        for temp_copy_path in self.temp_copies:
            if temp_copy_path.exists():
                try:
                    # Check if we have sudo access before attempting cleanup
                    result = subprocess.run([
                        "sudo", "-n", "test", "-d", str(temp_copy_path)
                    ], capture_output=True)
                    
                    if result.returncode == 0:
                        subprocess.run([
                            "sudo", "rm", "--recursive", "--force", "--one-file-system", str(temp_copy_path)
                        ], check=True)
                        print(f"Cleaned up temporary chroot: {temp_copy_path}")
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to clean up {temp_copy_path}: {e}")
    
    def setup_chroot(self):
        """Set up or update the build chroot"""
        if not self.chroot_path.exists():
            print(f"Creating chroot at {self.chroot_path}")
            self.chroot_path.mkdir(parents=True, exist_ok=True)
            self.build_utils.run_command([
                "sudo", "mkarchroot", 
                "-C", "chroot-config/pacman.conf",
                "-M", "chroot-config/makepkg.conf",
                str(self.chroot_path / "root"),
                "base-devel"
            ])
        
        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy pacman configuration
        chroot_pacman_conf = self.chroot_path / "root" / "etc" / "pacman.conf"
        if Path("chroot-config/pacman.conf").exists():
            self.build_utils.run_command([
                "sudo", "cp", "chroot-config/pacman.conf", str(chroot_pacman_conf)
            ])
        
        # Copy makepkg configuration
        chroot_makepkg_conf = self.chroot_path / "root" / "etc" / "makepkg.conf"
        if Path("chroot-config/makepkg.conf").exists():
            self.build_utils.run_command([
                "sudo", "cp", "chroot-config/makepkg.conf", str(chroot_makepkg_conf)
            ])
    
    def import_gpg_keys(self):
        """Import GPG keys from keys/pgp/ directory"""
        keys_dir = Path("keys/pgp")
        if keys_dir.exists():
            print("Importing GPG keys...")
            for key_file in keys_dir.glob("*.asc"):
                try:
                    subprocess.run(["gpg", "--import", str(key_file)], check=True)
                except subprocess.CalledProcessError as e:
                    print(f"ERROR: Failed to import GPG key {key_file}: {e}")
    
    def build_package(self, pkg_name, pkg_data):
        """Build a single package"""
        print(f"\n{'='*60}")
        print(f"Building {pkg_name}")
        print(f"{'='*60}")
        
        pkg_dir = Path("pkgbuilds") / pkg_name
        if not pkg_dir.exists():
            print(f"ERROR: Package directory {pkg_dir} not found")
            return False
        
        pkgbuild_path = pkg_dir / "PKGBUILD"
        if not pkgbuild_path.exists():
            print(f"ERROR: PKGBUILD not found at {pkgbuild_path}")
            return False
        
        # Clear cache if no-cache mode
        if self.no_cache:
            print("Clearing pacman cache...")
            if self.cache_dir.exists():
                self.build_utils.run_command([
                    "sudo", "rm", "-rf", str(self.cache_dir / "*")
                ])
        
        try:
            # Set SOURCE_DATE_EPOCH for reproducible builds
            env = os.environ.copy()
            env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
            
            # Check if package has checkdepends and uses !check
            checkdepends = []
            uses_nocheck = False
            
            with open(pkgbuild_path, 'r') as f:
                pkgbuild_content = f.read()
                if 'checkdepends=' in pkgbuild_content:
                    # Extract checkdepends (simplified parsing)
                    for line in pkgbuild_content.split('\n'):
                        if line.strip().startswith('checkdepends='):
                            # Simple extraction - would need proper bash parsing for complex cases
                            pass
                if '!check' in pkgbuild_content:
                    uses_nocheck = True
            
            # Build command
            cmd = [
                "makechrootpkg", "-c", "-u", "-r", str(self.chroot_path),
                "--", "--ignorearch"
            ]
            
            # Handle checkdepends if needed
            temp_copy_path = None
            if checkdepends and uses_nocheck:
                # Create temporary chroot copy
                temp_copy_name = f"temp-{pkg_name}"
                temp_copy_path = self.chroot_path / temp_copy_name
                self.temp_copies.append(temp_copy_path)
                
                print(f"Creating temporary chroot copy: {temp_copy_name}")
                # Use rsync with sudo like makechrootpkg does for non-btrfs
                subprocess.run([
                    "sudo", "rsync", "-a", "--delete", "-q", "-W", "-x", 
                    f"{self.chroot_path}/root/", str(temp_copy_path)
                ], check=True)
                
                # Always update chroot packages first
                print("Updating chroot packages...")
                subprocess.run([
                    "arch-nspawn", str(temp_copy_path),
                    "pacman", "-Syu", "--noconfirm"
                ], check=True, env=env)
                
                # Install checkdepends
                if checkdepends:
                    print(f"Installing checkdepends: {' '.join(checkdepends)}")
                    subprocess.run([
                        "arch-nspawn", str(temp_copy_path),
                        "pacman", "-S", "--noconfirm"
                    ] + checkdepends, check=True, env=env)
                
                # Use temporary copy
                cmd = [
                    "makechrootpkg", "-l", temp_copy_name, "-r", str(self.chroot_path),
                    "--", "--ignorearch"
                ]
            
            # Execute build
            print(f"Running: {' '.join(cmd)}")
            process = subprocess.run(
                cmd, cwd=pkg_dir, env=env, 
                capture_output=True, text=True
            )
            
            if process.returncode != 0:
                # Write build log on failure
                self.logs_dir.mkdir(exist_ok=True)
                timestamp = subprocess.run(['date', '+%Y%m%d-%H%M%S'], capture_output=True, text=True).stdout.strip()
                log_file = self.logs_dir / f"{pkg_name}-{timestamp}-build.log"
                self.build_utils.cleanup_old_logs(pkg_name)
                
                with open(log_file, 'w') as f:
                    f.write(f"Build failed for {pkg_name}\n")
                    f.write(f"Command: {' '.join(cmd)}\n")
                    f.write(f"Return code: {process.returncode}\n\n")
                    f.write("STDOUT:\n")
                    f.write(process.stdout)
                    f.write("\nSTDERR:\n")
                    f.write(process.stderr)
                
                print(f"ERROR: Build failed for {pkg_name}")
                print(f"Build log saved to: {log_file}")
                return False
            
            # Clean up temporary chroot copy
            if temp_copy_path and temp_copy_path in self.temp_copies:
                try:
                    # Check if we have sudo access before attempting cleanup
                    result = subprocess.run([
                        "sudo", "-n", "test", "-d", str(temp_copy_path)
                    ], capture_output=True)
                    
                    if result.returncode == 0:
                        # Use rm with sudo and --one-file-system like makechrootpkg does
                        subprocess.run([
                            "sudo", "rm", "--recursive", "--force", "--one-file-system", str(temp_copy_path)
                        ], check=True)
                        print(f"Cleaned up temporary chroot: {temp_copy_path}")
                    
                    self.temp_copies.remove(temp_copy_path)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to clean up temporary chroot {temp_copy_path}: {e}")
            
            # Upload packages if not disabled
            if not self.no_upload:
                target_repo = f"{pkg_data.get('repo', 'extra')}-testing"
                uploaded_count = self.build_utils.upload_packages(pkg_dir, target_repo)
                print(f"Successfully uploaded {uploaded_count} packages to {target_repo}")
            
            print(f"Successfully built {pkg_name}")
            return True
            
        except subprocess.CalledProcessError as e:
            # Write build log on failure
            self.logs_dir.mkdir(exist_ok=True)
            timestamp = subprocess.run(['date', '+%Y%m%d-%H%M%S'], capture_output=True, text=True).stdout.strip()
            log_file = self.logs_dir / f"{pkg_name}-{timestamp}-build.log"
            self.build_utils.cleanup_old_logs(pkg_name)
            
            with open(log_file, 'w') as f:
                f.write(f"Build failed for {pkg_name}\n")
                f.write(f"Exception: {e}\n")
            
            print(f"ERROR: Build failed for {pkg_name}: {e}")
            print(f"Build log saved to: {log_file}")
            return False
    
    def build_packages(self, packages_file, blacklist_file=None, continue_build=False):
        """Build all packages from JSON file"""
        # Load packages
        with open(packages_file, 'r') as f:
            data = json.load(f)
        
        packages = data.get('packages', [])
        if not packages:
            print("No packages to build")
            return
        
        # Apply blacklist filtering
        if blacklist_file:
            blacklist = load_blacklist(blacklist_file)
            if blacklist:
                packages, filtered_count = filter_blacklisted_packages(packages, blacklist)
                if filtered_count > 0:
                    print(f"Filtered out {filtered_count} blacklisted packages")
        
        # Filter out packages with skip=1
        packages = [pkg for pkg in packages if not pkg.get('skip', 0)]
        
        if not packages:
            print("No packages to build after filtering")
            return
        
        # Handle continue mode
        start_index = 0
        if continue_build:
            # Find last successful package (simplified - would need proper state tracking)
            print("Continue mode not fully implemented - starting from beginning")
        
        print(f"Building {len(packages)} packages...")
        
        # Set up build environment
        self.setup_chroot()
        self.import_gpg_keys()
        
        # Build packages
        failed_packages = []
        successful_packages = []
        
        for i, pkg in enumerate(packages[start_index:], start_index + 1):
            pkg_name = pkg['name']
            print(f"\n[{i}/{len(packages)}] Building {pkg_name}")
            
            if self.build_package(pkg_name, pkg):
                successful_packages.append(pkg_name)
            else:
                failed_packages.append(pkg)
        
        # Save failed packages for retry
        if failed_packages:
            failed_file = Path("failed_packages.json")
            with open(failed_file, 'w') as f:
                json.dump({
                    "_timestamp": data.get('_timestamp'),
                    "_command": data.get('_command'),
                    "packages": failed_packages
                }, f, indent=2)
            print(f"\nSaved {len(failed_packages)} failed packages to {failed_file}")
        
        # Summary
        print(f"\n{'='*60}")
        print(f"Build Summary:")
        print(f"  Successful: {len(successful_packages)}")
        print(f"  Failed: {len(failed_packages)}")
        print(f"{'='*60}")
        
        if failed_packages:
            print("Failed packages:")
            for pkg in failed_packages:
                print(f"  - {pkg['name']}")

def main():
    parser = argparse.ArgumentParser(description='Build Arch Linux packages for AArch64')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without executing')
    parser.add_argument('--json', default='packages_to_build.json',
                        help='JSON file with packages to build (default: packages_to_build.json)')
    parser.add_argument('--blacklist',
                        help='File containing packages to skip')
    parser.add_argument('--no-upload', action='store_true',
                        help='Build packages but don\'t upload to repository')
    parser.add_argument('--cache',
                        help='Custom pacman cache directory')
    parser.add_argument('--no-cache', action='store_true',
                        help='Clear cache before each package build')
    parser.add_argument('--continue', action='store_true', dest='continue_build',
                        help='Continue from last successful package')
    parser.add_argument('--chroot',
                        help='Custom chroot directory path')
    
    args = parser.parse_args()
    
    if not Path(args.json).exists():
        print(f"ERROR: Package file {args.json} not found")
        print("Run generate_build_list.py first to create the package list")
        sys.exit(1)
    
    builder = PackageBuilder(
        dry_run=args.dry_run,
        chroot_path=args.chroot,
        cache_dir=args.cache,
        no_cache=args.no_cache,
        no_upload=args.no_upload
    )
    
    try:
        builder.build_packages(
            args.json,
            args.blacklist,
            args.continue_build
        )
    except KeyboardInterrupt:
        print("\nBuild interrupted by user")
        builder._cleanup_temp_copies()
        sys.exit(1)

if __name__ == "__main__":
    main()
