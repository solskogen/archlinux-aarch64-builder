#!/usr/bin/env python3
"""
Bootstrap toolchain builder for Arch Linux packages.

Builds core toolchain packages (gcc, glibc, binutils, etc.) in a specific order
required for a complete toolchain rebuild. Has special handling for gcc/glibc
which must be manually checked out from special repositories.
"""
import subprocess
import os
import sys
import shutil
import argparse
import signal
from pathlib import Path
from build_utils import BuildUtils, BUILD_ROOT, CACHE_PATH

class BootstrapBuilder(BuildUtils):
    """Bootstrap toolchain package builder"""
    
    # Toolchain packages in build order (some repeated for multi-pass builds)
    TOOLCHAIN_PACKAGES = [
        "linux-api-headers", "glibc", "binutils", "gcc", "binutils", "gmp", "mpfr", 
        "libmpc", "libisl", "glibc", "gcc", "binutils", "gcc", "gmp", "mpfr", 
        "libmpc", "libisl", "libtool", "valgrind"
    ]
    
    REQUIRED_TOOLS = ['makechrootpkg', 'pkgctl', 'repo-upload', 'arch-nspawn']
    
    def __init__(self, chroot_path=BUILD_ROOT, cache_path=CACHE_PATH, dry_run=False, continue_build=False, no_update=False):
        super().__init__(dry_run)
        self.chroot_path = Path(chroot_path)
        self.cache_path = Path(cache_path)
        self.build_dir = Path("pkgbuilds")
        self.continue_build = continue_build
        self.progress_file = Path("bootstrap_progress.txt")
        self.no_update = no_update
    
    def get_start_index(self):
        """Get starting index for continue mode"""
        if not self.continue_build or not self.progress_file.exists():
            return 0
        
        try:
            last_completed = int(self.progress_file.read_text().strip())
            return last_completed + 1
        except (ValueError, FileNotFoundError):
            return 0
    
    def save_progress(self, index):
        """Save current progress"""
        if not self.dry_run:
            self.progress_file.write_text(str(index))
    
    def setup_environment(self):
        """Setup chroot environment for bootstrap"""
        print("Setting up bootstrap environment...")
        
        # Create directories
        self.build_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        
        # Setup chroot using shared utility
        self.setup_chroot(self.chroot_path, self.cache_path)
        """Setup chroot environment for bootstrap"""
        print("Setting up bootstrap environment...")
        
        # Create directories
        self.build_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        
        # Setup chroot using shared utility
        self.setup_chroot(self.chroot_path, self.cache_path)
    
    def bootstrap_build_package(self, pkg_name):
        """Build a single package in bootstrap mode"""
        pkg_dir = self.build_dir / pkg_name
        
        if not pkg_dir.exists():
            if pkg_name in ["gcc", "glibc"]:
                print(f"ERROR: {pkg_dir} not found - please checkout {pkg_name} from special repo manually")
                sys.exit(1)
        
        if self.dry_run:
            if pkg_name in ["gcc", "glibc"]:
                self.format_dry_run(f"Would bootstrap build {pkg_name} (special repo)", [
                    "Update chroot package database",
                    "Force install toolchain dependencies", 
                    "Clear pacman cache",
                    "Build with makechrootpkg",
                    "Upload to core-testing"
                ])
            else:
                self.format_dry_run(f"Would bootstrap build {pkg_name} (Arch repo)", [
                    "Update chroot package database",
                    "Force install toolchain dependencies", 
                    "Clear pacman cache",
                    "Build with makechrootpkg",
                    "Upload to core-testing"
                ])
            return True
        
        print(f"Bootstrap building {pkg_name}...")
        
        # Update chroot package database
        try:
            self.run_command([
                "arch-nspawn", str(self.chroot_path / "root"),
                "pacman", "-Sy", "--noconfirm"
            ])
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to update chroot package database: {e}")
            sys.exit(1)
        
        # Force install all toolchain dependencies in chroot
        all_toolchain = self.TOOLCHAIN_PACKAGES + ["gcc-libs"]
        try:
            self.run_command([
                "arch-nspawn", str(self.chroot_path / "root"),
                "pacman", "-S", "--noconfirm"
            ] + all_toolchain)
        except subprocess.CalledProcessError:
            # Ignore failures - some packages might not exist yet
            pass
        
        # Clear pacman cache
        try:
            for item in self.cache_path.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
        except Exception as e:
            print(f"ERROR: Failed to clear cache: {e}")
            sys.exit(1)
        
        # Build package
        try:
            env = os.environ.copy()
            env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
            
            process = subprocess.Popen([
                "makechrootpkg",
                "-r", str(self.chroot_path),
                "-d", str(self.cache_path),  # Use custom cache directory
                "-c",  # Clean chroot
                "-u",  # Update chroot before building
                "--", "--ignorearch",
            ], cwd=pkg_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, errors='replace', env=env)
            
            output_lines = []
            for line in process.stdout:
                print(line, end='')
                output_lines.append(line)
            
            process.wait()
            
            if process.returncode != 0:
                self.logs_dir.mkdir(exist_ok=True)
                timestamp = subprocess.run(['date', '+%Y%m%d-%H%M%S'], capture_output=True, text=True).stdout.strip()
                log_file = self.logs_dir / f"{pkg_name}-{timestamp}-build.log"
                self.cleanup_old_logs(pkg_name)
                with open(log_file, 'w') as f:
                    f.write(f"Bootstrap build failed for {pkg_name}\n")
                    f.write(f"Return code: {process.returncode}\n\n")
                    f.write("OUTPUT:\n")
                    f.write(''.join(output_lines))
                print(f"ERROR: Bootstrap build failed for {pkg_name} (return code: {process.returncode})")
                print(f"Build log written to {log_file}")
                sys.exit(1)
            
            # Upload to appropriate testing repository
            target_repo = "extra-testing" if pkg_name == "valgrind" else "core-testing"
            self.upload_packages(pkg_dir, target_repo)
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to bootstrap build {pkg_name}: {e}")
            sys.exit(1)
    
    def run_bootstrap(self):
        """Run complete bootstrap toolchain build"""
        print("=== Bootstrap Toolchain Build ===")
        print(f"Building {len(self.TOOLCHAIN_PACKAGES)} toolchain packages in order")
        
        # Atomic lock file creation with PID
        lock_file = Path(BUILD_ROOT) / "bootstrap.lock"
        Path(BUILD_ROOT).mkdir(parents=True, exist_ok=True)
        try:
            if not self.dry_run:
                # Check if lock file exists and if process is still running
                if lock_file.exists():
                    try:
                        lock_content = lock_file.read_text().strip()
                        if lock_content.startswith("PID:"):
                            old_pid = int(lock_content.split(":")[1])
                            # Check if process is still running
                            try:
                                os.kill(old_pid, 0)  # Signal 0 just checks if process exists
                                print("ERROR: Bootstrap already running (bootstrap.lock exists)")
                                print("If no other bootstrap is running, remove bootstrap.lock and try again")
                                sys.exit(1)
                            except OSError:
                                # Process doesn't exist, remove stale lock file
                                lock_file.unlink()
                                print("Removed stale bootstrap.lock file")
                    except (ValueError, IndexError):
                        # Invalid lock file format, remove it
                        lock_file.unlink()
                        print("Removed invalid bootstrap.lock file")
                
                # Create new lock file with current PID
                current_pid = os.getpid()
                lock_file.write_text(f"PID:{current_pid}\nBootstrap started at {subprocess.run(['date'], capture_output=True, text=True).stdout.strip()}")
        except FileExistsError:
            print("ERROR: Bootstrap already running (bootstrap.lock exists)")
            print("If no other bootstrap is running, remove bootstrap.lock and try again")
            sys.exit(1)
        
        # Set up signal handlers for graceful cleanup
        def signal_handler(signum, frame):
            print(f"\nReceived signal {signum}, cleaning up...")
            if not self.dry_run and lock_file.exists():
                lock_file.unlink()
            sys.exit(1)
        
        if not self.dry_run:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Validate required tools exist
            for tool in self.REQUIRED_TOOLS:
                if not self.dry_run and not shutil.which(tool):
                    print(f"ERROR: Required tool '{tool}' not found in PATH")
                    sys.exit(1)
            
            # Validate gcc/glibc directories exist before starting
            for pkg_name in ["gcc", "glibc"]:
                pkg_dir = self.build_dir / pkg_name
                if not pkg_dir.exists():
                    print(f"ERROR: {pkg_dir} not found - please checkout {pkg_name} from special repo manually")
                    sys.exit(1)
            
            # Ensure all toolchain PKGBUILDs are checked out before starting
            print("Checking out all toolchain PKGBUILDs...")
            unique_packages = list(dict.fromkeys(self.TOOLCHAIN_PACKAGES))  # Remove duplicates while preserving order
            for pkg_name in unique_packages:
                pkg_dir = self.build_dir / pkg_name
                
                if pkg_name in ["gcc", "glibc"]:
                    # Special repos - never update, user manages manually
                    if self.dry_run:
                        self.format_dry_run(f"Would skip update for {pkg_name} (special repo)", [])
                    else:
                        print(f"✓ Skipping update for {pkg_name} (special repo - managed manually)")
                    continue
                    # Special repos - just git pull
                    if self.dry_run:
                        self.format_dry_run(f"Would update {pkg_name} (special repo)", ["git stash", "git pull", "git stash pop"])
                    else:
                        try:
                            # Check if we're on a branch or detached HEAD
                            branch_result = self.run_command(["git", "branch", "--show-current"], cwd=pkg_dir, capture_output=True)
                            current_branch = branch_result.stdout.strip()
                            
                            if current_branch:
                                # We're on a branch, can pull normally
                                # Stash changes, pull, then restore
                                stash_result = self.run_command(["git", "stash"], cwd=pkg_dir, capture_output=True)
                                has_changes = "No local changes to save" not in stash_result.stdout
                                
                                self.run_command(["git", "pull"], cwd=pkg_dir)
                                
                                if has_changes:
                                    self.run_command(["git", "stash", "pop"], cwd=pkg_dir)
                            else:
                                # Detached HEAD - just fetch latest
                                print(f"Note: {pkg_name} is in detached HEAD state, skipping pull")
                            
                            print(f"✓ Updated {pkg_name}")
                        except subprocess.CalledProcessError as e:
                            print(f"Warning: git operations failed in {pkg_dir}: {e}")
                            print(f"✓ Continuing with existing {pkg_name} (ignoring git errors)")
                else:
                    # Regular Arch packages - clone or update
                    if pkg_dir.exists():
                        if self.dry_run:
                            self.format_dry_run(f"Would update {pkg_name}", ["git stash", "git pull", "git stash pop"])
                        else:
                            try:
                                # Check if we're on a branch or detached HEAD
                                branch_result = self.run_command(["git", "branch", "--show-current"], cwd=pkg_dir, capture_output=True)
                                current_branch = branch_result.stdout.strip()
                                
                                if current_branch:
                                    # We're on a branch, can pull normally
                                    # Stash changes, pull, then restore
                                    stash_result = self.run_command(["git", "stash"], cwd=pkg_dir, capture_output=True)
                                    has_changes = "No local changes to save" not in stash_result.stdout
                                    
                                    self.run_command(["git", "pull"], cwd=pkg_dir)
                                    
                                    if has_changes:
                                        self.run_command(["git", "stash", "pop"], cwd=pkg_dir)
                                else:
                                    # Detached HEAD - just fetch latest
                                    print(f"Note: {pkg_name} is in detached HEAD state, skipping pull")
                                
                                print(f"✓ Updated {pkg_name}")
                            except subprocess.CalledProcessError as e:
                                print(f"Warning: git operations failed in {pkg_dir}: {e}")
                                print(f"✓ Continuing with existing {pkg_name} (ignoring git errors)")
                    else:
                        if self.dry_run:
                            self.format_dry_run(f"Would clone {pkg_name}", [f"pkgctl repo clone {pkg_name}"])
                        else:
                            try:
                                self.run_command(["pkgctl", "repo", "clone", pkg_name], cwd=self.build_dir)
                                print(f"✓ Cloned {pkg_name}")
                            except subprocess.CalledProcessError as e:
                                print(f"ERROR: Failed to clone {pkg_name}: {e}")
                                sys.exit(1)
            
            self.setup_environment()
            
            # Validate chroot environment after setup
            chroot_root = self.chroot_path / "root"
            if not self.dry_run:
                if not chroot_root.exists():
                    print("ERROR: Chroot root directory not found - environment setup failed")
                    sys.exit(1)
                
                chroot_pacman = chroot_root / "usr" / "bin" / "pacman"
                if not chroot_pacman.exists():
                    print("ERROR: Chroot pacman not found - chroot environment is invalid")
                    sys.exit(1)
            
            # Clear pacman cache before starting bootstrap
            if self.dry_run:
                self.format_dry_run("Would clear pacman cache before bootstrap", [f"rm -rf {self.cache_path}/*"])
            else:
                try:
                    for item in self.cache_path.iterdir():
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)
                    print(f"Cleared cache directory before bootstrap: {self.cache_path}")
                except Exception as e:
                    print(f"ERROR: Failed to clear cache before bootstrap: {e}")
                    sys.exit(1)
            
            built_count = 0
            start_index = self.get_start_index()
            
            if start_index > 0:
                print(f"Continuing from package {start_index + 1}/{len(self.TOOLCHAIN_PACKAGES)}")
            
            for i, pkg_name in enumerate(self.TOOLCHAIN_PACKAGES, 1):
                if i - 1 < start_index:
                    print(f"Skipping {pkg_name} ({i}/{len(self.TOOLCHAIN_PACKAGES)}) - already completed")
                    continue
                    
                print(f"\n=== Building {pkg_name} ({i}/{len(self.TOOLCHAIN_PACKAGES)}) ===")
                self.bootstrap_build_package(pkg_name)
                built_count += 1
                self.save_progress(i - 1)
                print(f"✓ {pkg_name} built successfully")
            
            print(f"\n=== Bootstrap Summary ===")
            print(f"Successfully built: {built_count}/{len(self.TOOLCHAIN_PACKAGES) - start_index}")
            print("Toolchain bootstrap completed successfully!")
            
            # Clean up progress file on successful completion
            if not self.dry_run and self.progress_file.exists():
                self.progress_file.unlink()
            
        finally:
            # Remove lock file
            if not self.dry_run and lock_file.exists():
                lock_file.unlink()

def main():
    parser = argparse.ArgumentParser(description='Bootstrap build toolchain packages')
    parser.add_argument('--chroot', default=BUILD_ROOT,
                       help=f'Chroot path (default: {BUILD_ROOT})')
    parser.add_argument('--cache', default=CACHE_PATH,
                       help=f'Pacman cache directory (default: {CACHE_PATH})')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without actually building')
    parser.add_argument('--continue', action='store_true', dest='continue_build',
                       help='Continue from last successful package')
    parser.add_argument('--no-update', action='store_true',
                       help='Skip git updates for gcc/glibc special repos')
    
    args = parser.parse_args()
    
    builder = BootstrapBuilder(chroot_path=args.chroot, cache_path=args.cache, 
                              dry_run=args.dry_run, continue_build=args.continue_build,
                              no_update=args.no_update)
    builder.run_bootstrap()

if __name__ == "__main__":
    main()
