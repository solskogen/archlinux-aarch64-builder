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
import time
from pathlib import Path
from build_utils import BuildUtils, BUILD_ROOT, CACHE_PATH
from utils import (
    load_blacklist, filter_blacklisted_packages, 
    validate_package_name, safe_path_join, PACKAGE_SKIP_FLAG
)

class PackageBuilder:
    """
    Main package builder class that handles the complete build process.
    
    Manages chroot environments, dependency installation, package building,
    and cleanup operations. Supports dry-run mode and graceful interruption.
    """
    def __init__(self, dry_run=False, chroot_path=None, cache_dir=None, no_cache=False, no_upload=False, stop_on_failure=False):
        """
        Initialize the package builder.
        
        Args:
            dry_run: Show what would be done without executing
            chroot_path: Custom chroot directory path
            cache_dir: Custom pacman cache directory
            no_cache: Clear cache before each build
            no_upload: Build but don't upload packages
            stop_on_failure: Stop on first build failure
        """
        self.build_utils = BuildUtils(dry_run)
        self.dry_run = dry_run
        self.no_upload = no_upload
        self.no_cache = no_cache
        self.stop_on_failure = stop_on_failure
        self.chroot_path = Path(chroot_path) if chroot_path else Path(BUILD_ROOT)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(CACHE_PATH)
        self.logs_dir = Path("logs")
        self.temp_copies = []
        self.current_process = None
        
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
        
        # Copy pacman configuration
        chroot_pacman_conf = self.chroot_path / "root" / "etc" / "pacman.conf"
        if Path("chroot-config/pacman.conf").exists():
            self.build_utils.run_command([
                "sudo", "cp", "chroot-config/pacman.conf", str(chroot_pacman_conf)
            ])
        

    
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
    
    def build_package(self, pkg_name, pkg_data):
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
        
        # Parse PKGBUILD for depends, makedepends, and checkdepends
        # This section extracts dependency information from the PKGBUILD file
        # and handles various edge cases like comments, variables, and multi-line arrays
        depends = []
        makedepends = []
        checkdepends = []
        with open(pkgbuild_path, 'r') as f:
            pkgbuild_content = f.read()
            
            # Extract variables for substitution (e.g., _electron=electron37)
            # This handles cases where dependencies use variables like $_electron
            variables = {}
            for line in pkgbuild_content.split('\n'):
                line = line.strip()
                if '=' in line and not line.startswith('#') and not line.startswith('depends=') and not line.startswith('makedepends=') and not line.startswith('checkdepends='):
                    if line.startswith('_') or line.startswith('pkgver=') or line.startswith('pkgrel='):
                        var_name, var_value = line.split('=', 1)
                        variables[var_name] = var_value.strip('\'"')
            
            # Extract dependencies - handle single line and multi-line arrays
            # Supports various PKGBUILD formats including comments within arrays
            lines = pkgbuild_content.split('\n')
            in_depends = False
            in_makedepends = False
            in_checkdepends = False
            
            for line in lines:
                line = line.strip()
                
                # Handle depends
                if line.startswith('depends=('):
                    if line.endswith(')'):
                        deps_str = line[len('depends=('):-1]
                        depends.extend(self._parse_package_list(deps_str, variables))
                    else:
                        in_depends = True
                        deps_str = line[len('depends=('):]
                        depends.extend(self._parse_package_list(deps_str, variables))
                elif in_depends:
                    if line.endswith(')'):
                        deps_str = line[:-1]
                        depends.extend(self._parse_package_list(deps_str, variables))
                        in_depends = False
                    else:
                        # Skip comment lines entirely
                        if not line.startswith('#'):
                            depends.extend(self._parse_package_list(line, variables))
                
                # Handle makedepends
                elif line.startswith('makedepends=('):
                    if line.endswith(')'):
                        deps_str = line[len('makedepends=('):-1]
                        makedepends.extend(self._parse_package_list(deps_str, variables))
                    else:
                        in_makedepends = True
                        deps_str = line[len('makedepends=('):]
                        makedepends.extend(self._parse_package_list(deps_str, variables))
                elif in_makedepends:
                    if line.endswith(')'):
                        deps_str = line[:-1]
                        makedepends.extend(self._parse_package_list(deps_str, variables))
                        in_makedepends = False
                    else:
                        # Skip comment lines entirely
                        if not line.startswith('#'):
                            makedepends.extend(self._parse_package_list(line, variables))
                
                # Handle checkdepends
                elif line.startswith('checkdepends=('):
                    if line.endswith(')'):
                        deps_str = line[len('checkdepends=('):-1]
                        checkdepends.extend(self._parse_package_list(deps_str, variables))
                    else:
                        in_checkdepends = True
                        deps_str = line[len('checkdepends=('):]
                        checkdepends.extend(self._parse_package_list(deps_str, variables))
                elif in_checkdepends:
                    if line.endswith(')'):
                        deps_str = line[:-1]
                        checkdepends.extend(self._parse_package_list(deps_str, variables))
                        in_checkdepends = False
                    else:
                        # Skip comment lines entirely
                        if not line.startswith('#'):
                            checkdepends.extend(self._parse_package_list(line, variables))
        
        # Always create temporary chroot for build isolation
        # Each package gets its own temporary chroot copy to prevent dependency conflicts
        import random
        temp_id = random.randint(1000000, 9999999)
        temp_copy_name = f"temp-{pkg_name}-{temp_id}"
        temp_copy_path = self.chroot_path / temp_copy_name
        self.temp_copies.append(temp_copy_path)
        build_success = False
        
        try:
            # Always rsync root chroot to temporary chroot
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
            
            # Update package database in temporary chroot
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
            
            # Install all dependencies if any
            all_deps = depends + makedepends + checkdepends
            if all_deps:
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
            
            # Always build with temporary chroot
            cmd = [
                "makechrootpkg", "-l", temp_copy_name, "-r", str(self.chroot_path),
                "-d", str(self.cache_dir),
                "--", "--ignorearch"
            ]
            
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
            # Clean up temporary chroot copy (unless --stop-on-failure and build failed)
            if temp_copy_path in self.temp_copies:
                should_cleanup = True
                if self.stop_on_failure and not build_success:
                    should_cleanup = False
                    print(f"Preserving temporary chroot for debugging: {temp_copy_path}")
                
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
    
    def _parse_package_list(self, deps_str, variables=None):
        """
        Parse package list from PKGBUILD dependency arrays.
        
        Handles various formats including:
        - Single and double quoted package names
        - Comments within arrays (# comment)
        - Variable expansion ($_variable)
        - Version constraints (>=, >, =)
        - Brace expansion ({core,pthreads})
        
        Args:
            deps_str: Raw dependency string from PKGBUILD
            variables: Dictionary of variable substitutions
            
        Returns:
            list: Clean list of package names
        """
        if not deps_str.strip():
            return []
        
        if variables is None:
            variables = {}
        
        packages = []
        # Split by whitespace and clean up quotes
        for pkg in deps_str.split():
            pkg = pkg.strip().strip('\'"')
            # Skip comments and empty strings
            if pkg and not pkg.startswith('#'):
                # Expand variables
                for var_name, var_value in variables.items():
                    pkg = pkg.replace(f'${var_name}', var_value)
                
                # Handle brace expansion like libevent_{core,pthreads}-2.1.so
                if '{' in pkg and '}' in pkg:
                    import re
                    match = re.search(r'(.*){\s*([^}]+)\s*}(.*)', pkg)
                    if match:
                        prefix, options, suffix = match.groups()
                        for option in options.split(','):
                            expanded_pkg = prefix + option.strip() + suffix
                            # Remove version constraints
                            for op in ['>=', '>', '=']:
                                if op in expanded_pkg:
                                    expanded_pkg = expanded_pkg.split(op)[0]
                                    break
                            packages.append(expanded_pkg)
                        continue
                
                # Remove version constraints (>=, >, =)
                for op in ['>=', '>', '=']:
                    if op in pkg:
                        pkg = pkg.split(op)[0]
                        break
                packages.append(pkg)
        return packages
    
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
        packages = [pkg for pkg in packages if not pkg.get('skip', 0) == PACKAGE_SKIP_FLAG]
        
        if not packages:
            print("No packages to build after filtering")
            return
        
        # Handle continue mode
        start_index = 0
        if continue_build:
            # Look for last successful package in build logs
            last_successful = self._find_last_successful_package(packages)
            if last_successful is not None:
                start_index = last_successful + 1
                if start_index >= len(packages):
                    print("All packages already built successfully")
                    # Clear state file for next run
                    try:
                        Path("last_successful.txt").unlink()
                    except FileNotFoundError:
                        pass
                    return
                print(f"Continuing from package {start_index + 1}: {packages[start_index]['name']}")
            else:
                print("No previous successful builds found, starting from beginning")
        
        print(f"Building {len(packages)} packages...")
        
        # Set up build environment
        self.setup_chroot()
        self.import_gpg_keys()
        
        # Clean up old temporary chroots
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
        
        # Build packages
        failed_packages = []
        successful_packages = []
        
        for i, pkg in enumerate(packages[start_index:], start_index + 1):
            pkg_name = pkg['name']
            print(f"\n[{i}/{len(packages)}] Building {pkg_name}")
            
            try:
                if self.build_package(pkg_name, pkg):
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
    parser.add_argument('--stop-on-failure', action='store_true',
                        help='Stop building on first package failure')
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
        no_upload=args.no_upload,
        stop_on_failure=args.stop_on_failure
    )
    
    builder.build_packages(
        args.json,
        args.blacklist,
        args.continue_build
    )

if __name__ == "__main__":
    main()
