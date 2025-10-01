#!/usr/bin/env python3
"""
Package builder for Arch Linux AArch64 packages.

This script builds packages in clean chroot environments using makechrootpkg.
It handles dependency installation, temporary chroot management, and package uploads.

Key features:
- Clean chroot builds with dependency isolation
- Automatic dependency parsing from PKGBUILDs
- Variable expansion in dependency lists
- Comment filtering in PKGBUILD arrays
- Temporary chroot cleanup on interruption
- Package upload to testing repositories
"""

import json
import sys
import os
import subprocess
import signal
import argparse
from pathlib import Path
from utils import (
    load_blacklist, filter_blacklisted_packages, 
    validate_package_name, safe_path_join, PACKAGE_SKIP_FLAG,
    BuildUtils, BUILD_ROOT, CACHE_PATH
)

class PackageBuilder:
    """
    Main package builder class that handles the complete build process.
    
    Manages chroot environments, dependency installation, package building,
    and cleanup operations. Supports dry-run mode and graceful interruption.
    """
    def __init__(self, dry_run=False, chroot_path=None, cache_dir=None, no_cache=False, no_upload=False, stop_on_failure=False, preserve_chroot=False):
        """
        Initialize the package builder.
        
        Args:
            dry_run: Show what would be done without executing
            chroot_path: Custom chroot directory path
            cache_dir: Custom pacman cache directory
            no_cache: Clear cache before each build
            no_upload: Build but don't upload packages
            stop_on_failure: Stop on first build failure
            preserve_chroot: Preserve chroot even on successful builds
        """
        self.build_utils = BuildUtils(dry_run)
        self.dry_run = dry_run
        self.no_upload = no_upload
        self.no_cache = no_cache
        self.stop_on_failure = stop_on_failure
        self.preserve_chroot = preserve_chroot
        self.chroot_path = Path(chroot_path) if chroot_path else Path(BUILD_ROOT)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(CACHE_PATH)
        self.logs_dir = Path("logs")
        self.temp_copies = []
        self.current_process = None
        self.preserved_chroot = None
        
        # Set up signal handler for graceful cleanup
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """
        Graceful cleanup on Ctrl+C or termination signals.
        
        Terminates any running build process and cleans up temporary chroot copies
        to prevent leaving the system in an inconsistent state.
        """
        print(f"\nReceived signal {signum}, cleaning up...")
        if self.current_process:
            self.current_process.terminate()
            self.current_process.kill()
        self.cleanup_temp_copies()
        sys.exit(1)
    
    def _cleanup_temp_copies(self):
        """
        Clean up any temporary chroot copies created during builds.
        
        Uses sudo to remove temporary chroot directories that were created
        for dependency isolation. Handles permission errors gracefully.
        """
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
        """
        Set up or update the build chroot environment.
        
        Creates a clean chroot environment for building packages if it doesn't exist,
        or uses an existing one. Cleans up stale lock files and copies configuration.
        """
        # Clean up stale lock files
        if self.chroot_path.exists():
            for lock_file in self.chroot_path.glob("*.lock"):
                try:
                    lock_file.unlink()
                    print(f"Removed stale lock file: {lock_file}")
                except Exception:
                    pass
        
        # Setup chroot using shared utility
        self.build_utils.setup_chroot(self.chroot_path, self.cache_dir)
        

    
    def import_gpg_keys(self):
        """
        Import GPG keys from keys/pgp/ directory.
        
        Automatically imports any .asc GPG key files found in the keys/pgp/
        directory to enable signature verification during builds.
        """
        keys_dir = Path("keys/pgp")
        if keys_dir.exists():
            print("Importing GPG keys...")
            for key_file in keys_dir.glob("*.asc"):
                try:
                    subprocess.run(["gpg", "--import", str(key_file)], check=True)
                except subprocess.CalledProcessError as e:
                    print(f"ERROR: Failed to import GPG key {key_file}: {e}")
    
    def build_package(self, pkg_name, pkg_data, repackage=False):
        """
        Build a single package in a clean chroot environment.
        
        This is the core build method that:
        1. Validates the package name and paths
        2. Creates a temporary chroot copy for isolation
        3. Parses PKGBUILD dependencies with variable expansion
        4. Installs all dependencies in the temporary chroot
        5. Builds the package using makechrootpkg
        6. Uploads the built packages to the appropriate repository
        7. Cleans up temporary files
        
        Args:
            pkg_name: Name of the package to build
            pkg_data: Package metadata dictionary
            
        Returns:
            bool: True if build succeeded, False otherwise
        """
        # Validate package name
        if not validate_package_name(pkg_name):
            print(f"ERROR: Invalid package name: {pkg_name}")
            return False
            
        print(f"\n{'='*60}")
        print(f"Building {pkg_name}")
        print(f"{'='*60}")
        
        # Check that root chroot exists
        root_chroot = self.chroot_path / "root"
        if not root_chroot.exists():
            print(f"ERROR: Root chroot {root_chroot} does not exist")
            print("Run setup_chroot() first or create with mkarchroot")
            return False
        
        pkg_dir = safe_path_join(Path("pkgbuilds"), pkg_name)
        if not pkg_dir.exists():
            print(f"ERROR: Package directory {pkg_dir} not found")
            return False
        
        pkgbuild_path = pkg_dir / "PKGBUILD"
        if not pkgbuild_path.exists():
            print(f"ERROR: PKGBUILD not found at {pkgbuild_path}")
            return False
        
        # Import GPG keys if present
        keys_dir = pkg_dir / "keys" / "pgp"
        if keys_dir.exists():
            print("Importing GPG keys...")
            for key_file in keys_dir.glob("*.asc"):
                try:
                    subprocess.run(["gpg", "--import", str(key_file)], check=True)
                    print(f"Imported GPG key: {key_file.name}")
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to import GPG key {key_file}: {e}")
        
        # Clear cache if no-cache mode
        if self.no_cache:
            print("Clearing pacman cache...")
            if self.cache_dir.exists():
                try:
                    # Use find to delete all files and directories in cache
                    subprocess.run([
                        "sudo", "find", str(self.cache_dir), "-mindepth", "1", "-delete"
                    ], check=True)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to clear cache: {e}")
        
        # Set SOURCE_DATE_EPOCH for reproducible builds
        env = os.environ.copy()
        env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
        
        # Parse PKGBUILD for depends, makedepends, and checkdepends using bash
        depends = []
        makedepends = []
        checkdepends = []
        
        # Create a temporary script to source PKGBUILD and extract dependencies
        temp_script = f"""#!/bin/bash
cd "{pkg_dir}"
source PKGBUILD 2>/dev/null || exit 1
echo "DEPENDS_START"
printf '%s\\n' "${{depends[@]}}"
echo "DEPENDS_END"
echo "MAKEDEPENDS_START"
printf '%s\\n' "${{makedepends[@]}}"
echo "MAKEDEPENDS_END"
echo "CHECKDEPENDS_START"
printf '%s\\n' "${{checkdepends[@]}}"
echo "CHECKDEPENDS_END"
"""
        
        try:
            result = subprocess.run(['bash', '-c', temp_script], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                output = result.stdout
                current_section = None
                
                for line in output.split('\n'):
                    line = line.strip()
                    if line == "DEPENDS_START":
                        current_section = "depends"
                    elif line == "DEPENDS_END":
                        current_section = None
                    elif line == "MAKEDEPENDS_START":
                        current_section = "makedepends"
                    elif line == "MAKEDEPENDS_END":
                        current_section = None
                    elif line == "CHECKDEPENDS_START":
                        current_section = "checkdepends"
                    elif line == "CHECKDEPENDS_END":
                        current_section = None
                    elif line and current_section:
                        if current_section == "depends":
                            depends.append(line)
                        elif current_section == "makedepends":
                            makedepends.append(line)
                        elif current_section == "checkdepends":
                            checkdepends.append(line)
            else:
                print(f"Warning: Failed to parse PKGBUILD with bash: {result.stderr}")
        except subprocess.TimeoutExpired:
            print("Warning: PKGBUILD parsing timed out")
        except Exception as e:
            print(f"Warning: Error parsing PKGBUILD: {e}")
        
        # Always create temporary chroot for build isolation
        # Each package gets its own temporary chroot copy to prevent dependency conflicts
        
        # For repackage mode, reuse existing chroot if available
        if repackage and self.preserved_chroot and self.preserved_chroot.exists():
            temp_copy_path = self.preserved_chroot
            temp_copy_name = self.preserved_chroot.name
            print(f"Reusing preserved chroot: {temp_copy_name}")
        else:
            # Check if there's a preserved chroot from a previous failed build
            if repackage:
                # Look for any temp chroot that might be preserved
                temp_dirs = list(self.chroot_path.glob(f"temp-{pkg_name}-*"))
                if len(temp_dirs) == 1:
                    temp_copy_path = temp_dirs[0]
                    temp_copy_name = temp_copy_path.name
                    print(f"Found preserved chroot: {temp_copy_name}")
                elif len(temp_dirs) > 1:
                    print(f"ERROR: Multiple preserved chroots found for {pkg_name}:")
                    for temp_dir in temp_dirs:
                        print(f"  {temp_dir.name}")
                    print("Please remove the unwanted chroots and try again")
                    return False
                else:
                    print(f"ERROR: No preserved chroot found for {pkg_name}")
                    print(f"Repackage mode requires an existing chroot from a previous failed build")
                    return False
            else:
                import random
                temp_id = random.randint(1000000, 9999999)
                temp_copy_name = f"temp-{pkg_name}-{temp_id}"
                temp_copy_path = self.chroot_path / temp_copy_name
                self.temp_copies.append(temp_copy_path)
        
        build_success = False
        
        try:
            # Always rsync root chroot to temporary chroot unless repackaging
            if not repackage:
                print(f"Creating temporary chroot: {temp_copy_name}")
                try:
                    result = subprocess.run([
                        "sudo", "rsync", "-a", "--delete", "-q", "-W", "-x", 
                        f"{root_chroot}/", str(temp_copy_path) + "/"
                    ], check=True, capture_output=True, text=True)
                    print(f"Rsync completed successfully")
                except subprocess.CalledProcessError as e:
                    print(f"ERROR: Rsync failed: {e}")
                    print(f"Stderr: {e.stderr}")
                    return False
                except KeyboardInterrupt:
                    sys.exit(1)
            
            # Update package database in temporary chroot (skip in repackage mode)
            if not repackage:
                print("Updating package database in temporary chroot...")
                try:
                    subprocess.run([
                        "sudo", "arch-nspawn", 
                        "-c", str(self.cache_dir),  # Bind mount cache directory
                        str(temp_copy_path), "pacman", "-Suy", "--noconfirm"
                    ], check=True)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to update package database: {e}")
                except KeyboardInterrupt:
                    sys.exit(1)
            
            # Install all dependencies if any (skip in repackage mode)
            all_deps = depends + makedepends + checkdepends
            if all_deps and not repackage:
                print(f"Installing dependencies: {' '.join(all_deps)}")
                try:
                    subprocess.run([
                        "sudo", "arch-nspawn", 
                        "-c", str(self.cache_dir),  # Bind mount cache directory
                        str(temp_copy_path),
                        "pacman", "-S", "--noconfirm"
                    ] + all_deps, check=True, env=env)
                except KeyboardInterrupt:
                    sys.exit(1)
            elif repackage:
                print("Skipping dependency installation (repackage mode)")
            
            # Always build with temporary chroot
            cmd = [
                "makechrootpkg", "-l", temp_copy_name, "-r", str(self.chroot_path),
                "-d", str(self.cache_dir),
                "--", "--ignorearch"
            ]
            
            # Add repackage option if requested
            if repackage:
                cmd.append("-R")
                print("Using repackage mode (-R)")
                # Clear old packages from pkgdest in preserved chroot
                pkgdest_path = temp_copy_path / "pkgdest"
                if pkgdest_path.exists():
                    try:
                        subprocess.run(["sudo", "rm", "-rf", str(pkgdest_path)], check=True)
                        print("Cleared old packages from preserved chroot")
                    except subprocess.CalledProcessError as e:
                        print(f"Warning: Failed to clear pkgdest: {e}")
            
            # Execute build
            print(f"Running: {' '.join(cmd)}")
            try:
                # Stream output in real-time and capture for logging
                stdout_lines = []
                stderr_lines = []
                
                process = subprocess.Popen(cmd, cwd=pkg_dir, env=env, 
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                         text=True, bufsize=1, universal_newlines=True)
                
                # Read output line by line
                import select
                while process.poll() is None:
                    ready, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                    for stream in ready:
                        if stream == process.stdout:
                            line = stream.readline()
                            if line:
                                print(line, end='')
                                stdout_lines.append(line)
                        elif stream == process.stderr:
                            line = stream.readline()
                            if line:
                                print(line, end='', file=sys.stderr)
                                stderr_lines.append(line)
                
                # Read any remaining output
                remaining_stdout, remaining_stderr = process.communicate()
                if remaining_stdout:
                    print(remaining_stdout, end='')
                    stdout_lines.append(remaining_stdout)
                if remaining_stderr:
                    print(remaining_stderr, end='', file=sys.stderr)
                    stderr_lines.append(remaining_stderr)
                
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
                        if stdout_lines:
                            f.write("STDOUT:\n")
                            f.write(''.join(stdout_lines))
                            f.write("\n\n")
                        if stderr_lines:
                            f.write("STDERR:\n")
                            f.write(''.join(stderr_lines))
                            f.write("\n")
                    
                    print(f"ERROR: Build failed for {pkg_name}")
                    print(f"Build log saved to: {log_file}")
                    return False
                elif self.preserve_chroot:
                    # Save build log on success when preserving chroot
                    self.logs_dir.mkdir(exist_ok=True)
                    timestamp = subprocess.run(['date', '+%Y%m%d-%H%M%S'], capture_output=True, text=True).stdout.strip()
                    log_file = self.logs_dir / f"{pkg_name}-{timestamp}-build.log"
                    self.build_utils.cleanup_old_logs(pkg_name)
                    
                    with open(log_file, 'w') as f:
                        f.write(f"Build succeeded for {pkg_name}\n")
                        f.write(f"Command: {' '.join(cmd)}\n")
                        f.write(f"Return code: {process.returncode}\n\n")
                        if stdout_lines:
                            f.write("STDOUT:\n")
                            f.write(''.join(stdout_lines))
                            f.write("\n\n")
                        if stderr_lines:
                            f.write("STDERR:\n")
                            f.write(''.join(stderr_lines))
                            f.write("\n")
                    
                    print(f"Build log saved to: {log_file}")
                    
            except KeyboardInterrupt:
                print(f"\nBuild interrupted for {pkg_name}")
                sys.exit(1)
            
            # Upload packages if not disabled
            if not self.no_upload:
                target_repo = f"{pkg_data.get('repo', 'extra')}-testing"
                uploaded_count = self.build_utils.upload_packages(pkg_dir, target_repo)
                print(f"Successfully uploaded {uploaded_count} packages to {target_repo}")
            
            print(f"Successfully built {pkg_name}")
            self._update_last_successful(pkg_name)
            build_success = True
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to prepare build environment: {e}")
            return False
        finally:
            # Clean up temporary chroot copy (unless preserving or build failed with stop-on-failure)
            if temp_copy_path in self.temp_copies:
                should_cleanup = True
                if self.stop_on_failure and not build_success:
                    should_cleanup = False
                    print(f"Preserving temporary chroot for debugging: {temp_copy_path}")
                    # Also preserve for potential repackaging
                    self.preserved_chroot = temp_copy_path
                elif self.preserve_chroot:
                    should_cleanup = False
                    print(f"Preserving temporary chroot (--preserve-chroot): {temp_copy_path}")
                
                if should_cleanup:
                    try:
                        subprocess.run([
                            "sudo", "rm", "--recursive", "--force", "--one-file-system", str(temp_copy_path)
                        ], check=True)
                        self.temp_copies.remove(temp_copy_path)
                    except (subprocess.CalledProcessError, KeyboardInterrupt, Exception):
                        # Silent cleanup failure - don't let cleanup issues stop the build process
                        try:
                            self.temp_copies.remove(temp_copy_path)
                        except ValueError:
                            pass
            elif not build_success and not repackage:
                # Preserve chroot for potential repackaging on build failure
                self.preserved_chroot = temp_copy_path
                print(f"Preserving chroot for potential repackage: {temp_copy_path}")
            
            # Clean up lock files created during this build
            for lock_file in self.chroot_path.glob("*.lock"):
                try:
                    lock_file.unlink()
                except Exception:
                    pass
    
    def _find_last_successful_package(self, packages):
        """Find the index of the last successfully built package"""
        state_file = Path("last_successful.txt")
        if not state_file.exists():
            return None
        
        try:
            last_pkg_name = state_file.read_text().strip()
            for i, pkg in enumerate(packages):
                if pkg['name'] == last_pkg_name:
                    return i
        except Exception:
            pass
        
        return None
    
    def _update_last_successful(self, pkg_name):
        """Update the last successful package state file"""
        try:
            Path("last_successful.txt").write_text(pkg_name)
        except Exception:
            pass
    
    def build_packages(self, packages_file, blacklist_file=None, continue_build=False, repackage=False):
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
        packages = [pkg for pkg in packages if not pkg.get('skip', 0) == PACKAGE_SKIP_FLAG]
        
        if not packages:
            print("No packages to build after filtering")
            return
        
        # Handle continue mode
        start_index = 0
        repackage_next = False
        if continue_build:
            # Look for last successful package in build logs
            last_successful = self._find_last_successful_package(packages)
            if last_successful is not None:
                start_index = last_successful + 1
                repackage_next = repackage  # Only repackage the NEXT package
                if start_index >= len(packages):
                    print("All packages already built successfully")
                    # Clear state file for next run
                    try:
                        Path("last_successful.txt").unlink()
                    except FileNotFoundError:
                        pass
                    return
                next_pkg_name = packages[start_index]['name']
                if repackage:
                    print(f"Continuing from package {start_index + 1}: {next_pkg_name} (will repackage)")
                else:
                    print(f"Continuing from package {start_index + 1}: {next_pkg_name}")
            else:
                print("No previous successful builds found, starting from beginning")
        
        print(f"Building {len(packages)} packages...")
        
        # Set up build environment
        self.setup_chroot()
        self.import_gpg_keys()
        
        # Clean up old temporary chroots (skip in repackage mode or when preserving chroots)
        if not repackage and not self.preserve_chroot:
            print("Cleaning up old temporary chroots...")
            if self.dry_run:
                self.build_utils.format_dry_run("Would clean up old temporary chroots", [f"sudo rm -rf {self.chroot_path}/temp-*"])
            else:
                try:
                    temp_dirs = list(self.chroot_path.glob("temp-*"))
                    for temp_dir in temp_dirs:
                        subprocess.run(["sudo", "rm", "-rf", str(temp_dir)], check=True)
                    if temp_dirs:
                        print(f"Removed {len(temp_dirs)} old temporary chroots")
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to clean up some temporary chroots: {e}")
        elif repackage:
            print("Skipping cleanup of old temporary chroots (repackage mode)")
        elif self.preserve_chroot:
            print("Skipping cleanup of old temporary chroots (--preserve-chroot)")
        
        # Build packages
        failed_packages = []
        successful_packages = []
        
        for i, pkg in enumerate(packages[start_index:], start_index + 1):
            pkg_name = pkg['name']
            print(f"\n[{i}/{len(packages)}] Building {pkg_name}")
            
            # Use repackage only for the first package, then disable it
            use_repackage = repackage_next
            repackage_next = False  # Disable for subsequent packages
            
            try:
                if self.build_package(pkg_name, pkg, repackage=use_repackage):
                    successful_packages.append(pkg_name)
                else:
                    failed_packages.append(pkg)
                    if self.stop_on_failure:
                        print(f"Stopping build process due to failure in {pkg_name}")
                        break
            except Exception as e:
                print(f"DEBUG: Unexpected exception in build loop: {e}")
                failed_packages.append(pkg)
                if self.stop_on_failure:
                    print(f"Stopping build process due to exception in {pkg_name}")
                    break
        
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
    parser.add_argument('--blacklist', default='blacklist.txt',
                        help='File containing packages to skip (default: blacklist.txt)')
    parser.add_argument('--no-upload', action='store_true',
                        help='Build packages but don\'t upload to repository')
    parser.add_argument('--cache',
                        help='Custom pacman cache directory')
    parser.add_argument('--no-cache', action='store_true',
                        help='Clear cache before each package build')
    parser.add_argument('--continue', action='store_true', dest='continue_build',
                        help='Continue from last successful package')
    parser.add_argument('--repackage', action='store_true',
                        help='Repackage the next package to build (requires --continue)')
    parser.add_argument('--preserve-chroot', action='store_true',
                        help='Preserve chroot even on successful builds')
    parser.add_argument('--stop-on-failure', action='store_true',
                        help='Stop building on first package failure')
    parser.add_argument('--chroot',
                        help='Custom chroot directory path')
    
    args = parser.parse_args()
    
    # Validate repackage option
    if args.repackage and not args.continue_build:
        print("ERROR: --repackage requires --continue")
        sys.exit(1)
    
    if not Path(args.json).exists():
        print(f"ERROR: Package file {args.json} not found")
        print("Run generate_build_list.py first to create the package list")
        sys.exit(1)
    
    builder = PackageBuilder(
        dry_run=args.dry_run,
        chroot_path=args.chroot,
        cache_dir=args.cache,
        no_cache=args.no_cache,
        no_upload=args.no_upload,
        stop_on_failure=args.stop_on_failure,
        preserve_chroot=args.preserve_chroot
    )
    
    builder.build_packages(
        args.json,
        args.blacklist,
        args.continue_build,
        args.repackage
    )

if __name__ == "__main__":
    main()
