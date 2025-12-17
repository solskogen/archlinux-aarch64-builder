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
    BuildUtils, BUILD_ROOT, CACHE_PATH, TEMP_CHROOT_ID_MIN, TEMP_CHROOT_ID_MAX, 
    SEPARATOR_WIDTH, GIT_COMMAND_TIMEOUT, import_gpg_keys, upload_packages,
    safe_command_execution
)

class PackageBuilder:
    """
    Main package builder class that handles the complete build process.
    
    Manages chroot environments, dependency installation, package building,
    and cleanup operations. Supports dry-run mode and graceful interruption.
    """
    def __init__(self, dry_run=False, chroot_path=None, cache_dir=None, no_cache=False, no_upload=False, stop_on_failure=False, preserve_chroot=False, cleanup_on_failure=False):
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
            cleanup_on_failure: Delete temporary chroots even on build failure
        """
        self.build_utils = BuildUtils(dry_run)
        self.dry_run = dry_run
        self.no_upload = no_upload
        self.no_cache = no_cache
        self.stop_on_failure = stop_on_failure
        self.preserve_chroot = preserve_chroot
        self.cleanup_on_failure = cleanup_on_failure
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
        

    
    def _validate_build_inputs(self, pkg_name, pkg_data):
        """Validate package name and required paths"""
        if not validate_package_name(pkg_name):
            print(f"ERROR: Invalid package name: {pkg_name}")
            return False
            
        root_chroot = self.chroot_path / "root"
        if not root_chroot.exists():
            print(f"ERROR: Root chroot {root_chroot} does not exist")
            print("Run setup_chroot() first or create with mkarchroot")
            return False
        
        pkg_dir = safe_path_join(Path("pkgbuilds"), pkg_data.get('basename', pkg_name))
        if not pkg_dir.exists():
            print(f"ERROR: Package directory {pkg_dir} not found")
            return False
        
        pkgbuild_path = pkg_dir / "PKGBUILD"
        if not pkgbuild_path.exists():
            print(f"ERROR: PKGBUILD not found at {pkgbuild_path}")
            return False
            
        return True

    def _setup_temp_chroot(self, pkg_name):
        """Create temporary chroot for package"""
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        temp_copy_name = f"temp-{pkg_name}-{timestamp}"
        temp_copy_path = self.chroot_path / temp_copy_name
        while temp_copy_path.exists():
            # If timestamp collision, add microseconds
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")[:14]
            temp_copy_name = f"temp-{pkg_name}-{timestamp}"
            temp_copy_path = self.chroot_path / temp_copy_name
        self.temp_copies.append(temp_copy_path)
        return temp_copy_path

    def _prepare_build_environment(self, temp_copy_path, pkg_name, pkg_dir):
        """Setup chroot environment and install dependencies"""
        root_chroot = self.chroot_path / "root"
        
        # Import GPG keys
        keys_dir = pkg_dir / "keys" / "pgp"
        if keys_dir.exists():
            print("Importing GPG keys...")
            for key_file in keys_dir.glob("*.asc"):
                try:
                    subprocess.run(["gpg", "--import", str(key_file)], check=True)
                    print(f"Imported GPG key: {key_file.name}")
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to import GPG key {key_file}: {e}")
        
        # Clear cache if needed
        if self.no_cache:
            print("Clearing pacman cache...")
            if self.cache_dir.exists():
                try:
                    subprocess.run([
                        "sudo", "find", str(self.cache_dir), "-mindepth", "1", "-delete"
                    ], check=True)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to clear cache: {e}")
        
        # Create temp chroot
        print(f"Creating temporary chroot: {temp_copy_path.name}")
        try:
            subprocess.run([
                "sudo", "rsync", "-a", "--delete", "-q", "-W", "-x", 
                f"{root_chroot}/", str(temp_copy_path) + "/"
            ], check=True, capture_output=True, text=True, errors='replace')
            print("Rsync completed successfully")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Rsync failed: {e}")
        
        # Update package database
        print("Updating package database in temporary chroot...")
        try:
            subprocess.run([
                "sudo", "arch-nspawn", 
                "-c", str(self.cache_dir),
                str(temp_copy_path), "pacman", "-Suy", "--noconfirm"
            ], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to update package database: {e}")
        
        # Parse and install only checkdepends (makechrootpkg handles depends/makedepends)
        depends, makedepends, checkdepends = self._parse_pkgbuild_deps(pkg_dir)
        if checkdepends:
            print(f"Installing checkdepends: {' '.join(checkdepends)}")
            try:
                env = os.environ.copy()
                env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
                subprocess.run([
                    "sudo", "arch-nspawn", 
                    "-c", str(self.cache_dir),
                    str(temp_copy_path),
                    "pacman", "-S", "--noconfirm"
                ] + checkdepends, check=True, env=env)
            except KeyboardInterrupt:
                sys.exit(1)

    def _parse_pkgbuild_deps(self, pkg_dir):
        """Parse PKGBUILD dependencies using bash"""
        import shlex
        temp_script = f"""#!/bin/bash
cd {shlex.quote(str(pkg_dir))}
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
        
        depends = []
        makedepends = []
        checkdepends = []
        
        try:
            result = subprocess.run(['bash', '-c', temp_script], 
                                  capture_output=True, text=True, timeout=GIT_COMMAND_TIMEOUT,
                                  errors='replace')
            if result.returncode == 0:
                current_section = None
                for line in result.stdout.split('\n'):
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
        
        return depends, makedepends, checkdepends

    def _execute_build(self, pkg_name, pkg_data, temp_copy_path, pkg_dir, log_file):
        """Execute the actual package build"""
        env = os.environ.copy()
        env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
        
        cmd = [
            "makechrootpkg", "-l", temp_copy_path.name, "-r", str(self.chroot_path),
            "-d", str(self.cache_dir), "-t", "/tmp:size=128G",
            "--", "--ignorearch"
        ]
        
        print(f"Running: {' '.join(cmd)}")
        
        # Get formatted start time
        start_time = subprocess.run(['date', '+%a %b %d %H:%M:%S %Y'], capture_output=True, text=True).stdout.strip()
        version = pkg_data.get('version', 'unknown')
        
        build_success = False
        try:
            with open(log_file, 'w') as f:
                # Write start header
                f.write(f"==> Build started: {pkg_name} {version} ({start_time})\n")
                f.flush()
                
                process = subprocess.Popen(cmd, cwd=pkg_dir, env=env, 
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                         text=True, bufsize=1, errors='replace')
                
                # Stream output to both console and log file
                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        print(line, end='')
                        f.write(line)
                        f.flush()
                
                process.wait()
                
                if process.returncode != 0:
                    f.write(f"\nBuild failed with return code: {process.returncode}\n")
                    raise subprocess.CalledProcessError(process.returncode, cmd)
                
                build_success = True
                    
        except subprocess.CalledProcessError:
            error_msg = BuildError.format_build_failure(pkg_name, log_file, "Package compilation failed")
            print(error_msg)
            return False
        except Exception as e:
            error_msg = BuildError.format_build_failure(pkg_name, log_file, f"Build setup failed: {e}")
            print(error_msg)
            return False
        except KeyboardInterrupt:
            print(f"\nBuild interrupted for {pkg_name}")
            sys.exit(1)
        finally:
            # Always write end footer
            end_time = subprocess.run(['date', '+%a %b %d %H:%M:%S %Y'], capture_output=True, text=True).stdout.strip()
            status = "SUCCESS" if build_success else "FAILED"
            try:
                with open(log_file, 'a') as f:
                    f.write(f"==> Build finished: {pkg_name} {version} ({end_time}) [{status}]\n")
            except Exception:
                pass  # Don't fail if we can't write the footer
        
        # Upload packages
        if not self.no_upload:
            repo = pkg_data.get('repo', 'unknown')
            if repo in ['core', 'extra']:
                target_repo = f"{repo}-testing"
            else:
                target_repo = 'forge'
            uploaded_count = upload_packages(pkg_dir, target_repo, self.dry_run)
            print(f"Successfully uploaded {uploaded_count} packages to {target_repo}")
            
            # Clear built packages from cache to prevent stale/corrupted cache issues
            if not self.dry_run:
                self._clear_cycle_packages_from_cache(pkg_name, pkg_dir)
        
        print(f"Successfully built {pkg_name}")
        self._update_last_successful(pkg_name)
        return True

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
        except Exception as e:
            print(f"Warning: Failed to update last successful package: {e}")
    
    def _clear_cache(self):
        """Clear pacman cache to force using newly uploaded packages"""
        cache_dir = Path(self.cache_dir)
        if cache_dir.exists():
            try:
                import shutil
                shutil.rmtree(cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)
                print(f"Cleared cache directory: {cache_dir}")
            except Exception as e:
                print(f"Warning: Failed to clear cache: {e}")

    def _should_skip_due_to_failed_dependencies(self, pkg, failed_packages, provides_map):
        """Check if package should be skipped due to failed dependencies"""
        if not failed_packages:
            return False, []
        
        failed_names = {fp['name'] for fp in failed_packages}
        
        # Check all dependencies
        all_deps = []
        for dep_type in ['depends', 'makedepends', 'checkdepends']:
            all_deps.extend(pkg.get(dep_type, []))
        
        failed_deps = []
        for dep_str in all_deps:
            # Extract package name (remove version constraints)
            dep_name = dep_str.split('=')[0].split('>')[0].split('<')[0].strip()
            
            # Check direct dependency
            if dep_name in failed_names:
                failed_deps.append(dep_name)
                continue
            
            # Check provides relationships
            if dep_name in provides_map:
                provider = provides_map[dep_name]
                if provider in failed_names:
                    failed_deps.append(f"{dep_name} (provided by {provider})")
        
        return len(failed_deps) > 0, failed_deps

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
        
        # Build provides mapping for dependency checking
        provides_map = {}
        for pkg in packages:
            pkg_name = pkg['name']
            provides_map[pkg_name] = pkg_name  # Self-reference
            for provide in pkg.get('provides', []):
                provide_name = provide.split('=')[0].strip()
                provides_map[provide_name] = pkg_name
        
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
                next_pkg_name = packages[start_index]['name']
                print(f"Continuing from package {start_index + 1}: {next_pkg_name}")
            else:
                print("No previous successful builds found, starting from beginning")
        
        print(f"Building {len(packages)} packages...")
        
        # Set up build environment
        self.setup_chroot()
        import_gpg_keys()
        
        # Clean up old temporary chroots (skip when preserving chroots)
        if not self.preserve_chroot:
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
        else:
            print("Skipping cleanup of old temporary chroots (--preserve-chroot)")
        
        # Build packages
        failed_packages = []
        successful_packages = []
        current_cycle_group = None
        cycle_stage_1_success = {}  # Track which packages succeeded in stage 1 of each cycle
        
        # When continuing, populate cycle_stage_1_success with packages that were already built
        if continue_build:
            last_successful_index = self._find_last_successful_package(packages)
            if last_successful_index is not None:
                for pkg in packages[:last_successful_index + 1]:
                    cycle_group = pkg.get('cycle_group')
                    cycle_stage = pkg.get('cycle_stage')
                    if cycle_group is not None and cycle_stage == 1:
                        if cycle_group not in cycle_stage_1_success:
                            cycle_stage_1_success[cycle_group] = set()
                        cycle_stage_1_success[cycle_group].add(pkg['name'])
        
        # Build cycle info map for display
        cycle_info = {}
        for pkg in packages:
            cycle_group = pkg.get('cycle_group')
            if cycle_group is not None:
                if cycle_group not in cycle_info:
                    cycle_info[cycle_group] = set()
                cycle_info[cycle_group].add(pkg['name'])
        
        for i, pkg in enumerate(packages[start_index:], start_index + 1):
            pkg_name = pkg['name']
            cycle_group = pkg.get('cycle_group')
            cycle_stage = pkg.get('cycle_stage')
            
            # Handle cycle stage transitions
            if cycle_group is not None:
                cycle_packages = sorted(cycle_info[cycle_group])
                cycle_desc = f"Cycle {cycle_group + 1} ({' ↔ '.join(cycle_packages)})"
                
                if cycle_stage == 1:
                    print(f"\n[{i}/{len(packages)}] Building {pkg_name} ({cycle_desc}, Stage 1/2)")
                elif cycle_stage == 2:
                    # Check if this package succeeded in stage 1
                    if cycle_group not in cycle_stage_1_success or pkg_name not in cycle_stage_1_success[cycle_group]:
                        print(f"\n[{i}/{len(packages)}] Skipping {pkg_name} ({cycle_desc}, Stage 2/2) - failed in Stage 1")
                        failed_packages.append(pkg)
                        continue
                    
                    print(f"\n[{i}/{len(packages)}] Building {pkg_name} ({cycle_desc}, Stage 2/2)")
            else:
                print(f"\n[{i}/{len(packages)}] Building {pkg_name}")
            
            # Check if package should be skipped due to failed dependencies
            should_skip, failed_deps = self._should_skip_due_to_failed_dependencies(pkg, failed_packages, provides_map)
            if should_skip:
                print(f"Skipping {pkg_name} - depends on failed packages: {', '.join(failed_deps)}")
                failed_packages.append(pkg)
                continue
            
            try:
                if self.build_package(pkg_name, pkg):
                    successful_packages.append(pkg_name)
                    
                    # Track cycle stage 1 successes
                    if cycle_group is not None and cycle_stage == 1:
                        if cycle_group not in cycle_stage_1_success:
                            cycle_stage_1_success[cycle_group] = set()
                        cycle_stage_1_success[cycle_group].add(pkg_name)
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
        print(f"\n{'='*SEPARATOR_WIDTH}")
        print(f"Build Summary:")
        print(f"  Successful: {len(successful_packages)}")
        print(f"  Failed: {len(failed_packages)}")
        print(f"{'='*SEPARATOR_WIDTH}")
        
        if failed_packages:
            print("Failed packages:")
            for pkg in failed_packages:
                print(f"  - {pkg['name']}")
            return 1
        return 0

    def _get_package_filenames_from_pkgbuild(self, pkg_dir):
        """Extract exact package filenames that will be built from PKGBUILD"""
        try:
            pkgbuild_path = pkg_dir / "PKGBUILD"
            if not pkgbuild_path.exists():
                return []
            
            from utils import get_target_architecture
            target_arch = get_target_architecture()
            
            import shlex
            temp_script = f"""#!/bin/bash
cd {shlex.quote(str(pkg_dir))}
source PKGBUILD 2>/dev/null || exit 1
CARCH="{target_arch}"
for pkg in "${{pkgname[@]}}"; do
    fullver="$pkgver-$pkgrel"
    if [[ -n $epoch ]]; then
        fullver="$epoch:$fullver"
    fi
    echo "$pkg-$fullver-$CARCH.pkg.tar.zst"
    echo "$pkg-$fullver-$CARCH.pkg.tar.zst.sig"
done
"""
            
            result = subprocess.run(['bash', '-c', temp_script], 
                                  capture_output=True, text=True, timeout=10, 
                                  errors='replace')
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.split('\n') if line.strip()]
            else:
                return []
        except Exception as e:
            print(f"Warning: Failed to extract package filenames from PKGBUILD: {e}")
            return []

    def _clear_cycle_packages_from_cache(self, pkg_name, pkg_dir):
        """Clear cycle packages from pacman cache after first build"""
        try:
            package_filenames = self._get_package_filenames_from_pkgbuild(pkg_dir)
            if not package_filenames:
                return
            
            print(f"Clearing cycle packages from cache: {pkg_name}")
            
            # Remove exact package files from cache
            for filename in package_filenames:
                cache_file = self.cache_dir / filename
                if cache_file.exists():
                    try:
                        cache_file.unlink()
                        print(f"  Removed: {filename}")
                    except Exception as e:
                        print(f"  Warning: Failed to remove {filename}: {e}")
                        
        except Exception as e:
            print(f"Warning: Failed to clear cycle packages from cache: {e}")

    def build_package(self, pkg_name, pkg_data):
        """Build a single package in a clean chroot environment"""
        # Dry-run mode
        if self.dry_run:
            print(f"\n{'='*SEPARATOR_WIDTH}")
            print(f"[DRY RUN] Would build {pkg_name}")
            print(f"{'='*SEPARATOR_WIDTH}")
            
            pkg_dir = safe_path_join(Path("pkgbuilds"), pkg_data.get('basename', pkg_name))
            if not pkg_dir.exists():
                print(f"[DRY RUN] ERROR: Package directory {pkg_dir} does not exist")
                return False
            
            pkgbuild_path = pkg_dir / "PKGBUILD"
            if not pkgbuild_path.exists():
                print(f"[DRY RUN] ERROR: PKGBUILD not found at {pkgbuild_path}")
                return False
            
            print(f"[DRY RUN] Would build package from {pkg_dir}")
            print(f"[DRY RUN] Would upload to {pkg_data.get('repo', 'extra')}-testing repository")
            return True
        
        print(f"\n{'='*SEPARATOR_WIDTH}")
        print(f"Building {pkg_name}")
        print(f"{'='*SEPARATOR_WIDTH}")
        
        # Validate inputs
        if not self._validate_build_inputs(pkg_name, pkg_data):
            return False
        
        pkg_dir = safe_path_join(Path("pkgbuilds"), pkg_data.get('basename', pkg_name))
        
        # Setup temp chroot
        try:
            temp_copy_path = self._setup_temp_chroot(pkg_name)
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return False
        
        build_success = False
        
        # Create log file early so we can reference it in all error cases
        timestamp = subprocess.run(['date', '+%Y%m%d-%H%M%S'], capture_output=True, text=True).stdout.strip()
        log_file = self.logs_dir / f"{pkg_name}-{timestamp}-build.log"
        self.build_utils.cleanup_old_logs(pkg_name)
        
        try:
            # Prepare build environment
            self._prepare_build_environment(temp_copy_path, pkg_name, pkg_dir)
            
            # Execute build
            build_success = self._execute_build(pkg_name, pkg_data, temp_copy_path, pkg_dir, log_file)
            return build_success
            
        except Exception as e:
            error_msg = BuildError.format_setup_failure(pkg_name, str(e), log_file)
            print(error_msg)
            return False
        finally:
            self._cleanup_temp_chroot(temp_copy_path, not build_success)
            
            # Clean up lock files
            for lock_file in self.chroot_path.glob("*.lock"):
                try:
                    lock_file.unlink()
                except Exception as e:
                    print(f"Warning: Failed to remove lock file {lock_file}: {e}")

    def _cleanup_temp_chroot(self, temp_copy_path, build_failed=False):
        """Clean up temporary chroot"""
        if temp_copy_path in self.temp_copies:
            should_cleanup = True
            if self.preserve_chroot or (build_failed and not self.cleanup_on_failure):
                should_cleanup = False
                if self.preserve_chroot:
                    reason = "preserve-chroot flag"
                else:
                    reason = "build failed"
                print(f"Preserving temporary chroot: {temp_copy_path} ({reason})")
            
            if should_cleanup:
                try:
                    subprocess.run([
                        "sudo", "rm", "--recursive", "--force", "--one-file-system", str(temp_copy_path)
                    ], check=True)
                    self.temp_copies.remove(temp_copy_path)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to cleanup chroot {temp_copy_path}: {e}")
                except Exception as e:
                    print(f"Error during cleanup: {e}")
                finally:
                    try:
                        self.temp_copies.remove(temp_copy_path)
                    except ValueError:
                        pass


class BuildError:
    """Centralized error message formatting"""
    
    @staticmethod
    def format_build_failure(pkg_name: str, log_file: Path, context: str = "") -> str:
        return f"""
BUILD FAILED: {pkg_name}
{f'Context: {context}' if context else ''}
Log file: {log_file}
Troubleshooting:
  - Check build log for detailed error messages
  - Run with --preserve-chroot to debug in chroot environment
  - Verify all dependencies are available
  - Check for architecture-specific build issues
"""

    @staticmethod
    def format_setup_failure(pkg_name: str, error: str, log_file: Path = None) -> str:
        log_info = f"\nLog file: {log_file}" if log_file else ""
        return f"""
SETUP FAILED: {pkg_name}
Error: {error}{log_info}
Troubleshooting:
  - Ensure chroot environment is properly initialized
  - Check disk space and permissions
  - Verify network connectivity for dependency downloads
"""
        """Clean up temporary chroot"""
        if temp_copy_path in self.temp_copies:
            should_cleanup = True
            if self.stop_on_failure or self.preserve_chroot:
                should_cleanup = False
                print(f"Preserving temporary chroot: {temp_copy_path}")
            
            if should_cleanup:
                try:
                    subprocess.run([
                        "sudo", "rm", "--recursive", "--force", "--one-file-system", str(temp_copy_path)
                    ], check=True)
                    self.temp_copies.remove(temp_copy_path)
                except subprocess.CalledProcessError as e:
                    print(f"Warning: Failed to cleanup chroot {temp_copy_path}: {e}")
                except Exception as e:
                    print(f"Error during cleanup: {e}")
                finally:
                    try:
                        self.temp_copies.remove(temp_copy_path)
                    except ValueError:
                        pass
                    try:
                        lock_file.unlink()
                    except Exception as e:
                        print(f"Warning: Failed to remove lock file {lock_file}: {e}")


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
    parser.add_argument('--preserve-chroot', action='store_true',
                        help='Preserve chroot even on successful builds')
    parser.add_argument('--cleanup-on-failure', action='store_true',
                        help='Delete temporary chroots even on build failure')
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
        stop_on_failure=args.stop_on_failure,
        preserve_chroot=args.preserve_chroot,
        cleanup_on_failure=args.cleanup_on_failure
    )
    
    exit_code = builder.build_packages(
        args.json,
        args.blacklist,
        args.continue_build
    )
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
