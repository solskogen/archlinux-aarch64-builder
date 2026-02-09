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
from utils import BuildUtils, BUILD_ROOT, CACHE_PATH, upload_packages, get_target_architecture

# Special repository configuration
GCC_REPO_URL = "https://gitlab.archlinux.org/solskogen/gcc.git"
GCC_BRANCH = "experimental"

# Toolchain configuration - staged build
STAGE1_PACKAGES = [
    "linux-api-headers", "glibc", "binutils", "gcc", "gmp", "mpfr", "libmpc", "libisl"
]

STAGE2_PACKAGES = [
    "glibc", "binutils", "gcc", "gmp", "mpfr", "libmpc", "libisl", "libtool", "valgrind"
]

ALL_BOOTSTRAP_PACKAGES = list(dict.fromkeys(STAGE1_PACKAGES + STAGE2_PACKAGES))

REQUIRED_TOOLS = ['makechrootpkg', 'pkgctl', 'repo-upload', 'arch-nspawn']

class BootstrapBuilder(BuildUtils):
    """Bootstrap toolchain package builder"""
    
    def __init__(self, chroot_path=BUILD_ROOT, cache_path=CACHE_PATH, dry_run=False, continue_build=False, start_from=None):
        super().__init__(dry_run)
        self.chroot_path = Path(chroot_path)
        self.cache_path = Path(cache_path)
        self.build_dir = Path("pkgbuilds")
        self.continue_build = continue_build
        self.progress_file = Path("bootstrap_progress.txt")
        self.start_from = start_from
        self.target_arch = get_target_architecture()
    
    def get_start_index(self, packages):
        """Get starting index for continue mode or start_from option"""
        if self.start_from:
            try:
                return packages.index(self.start_from)
            except ValueError:
                print(f"ERROR: Package '{self.start_from}' not found in package list")
                sys.exit(1)
        
        if not self.continue_build or not self.progress_file.exists():
            return 0
        
        try:
            last_completed = int(self.progress_file.read_text().strip())
            return last_completed + 1
        except (ValueError, FileNotFoundError):
            return 0
    
    def save_progress(self, index):
        """Save build progress for continue mode"""
        self.progress_file.write_text(str(index))

    def setup_environment(self):
        """Setup chroot environment for bootstrap"""
        print("Setting up bootstrap environment...")
        
        # Create directories
        self.build_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        
        # Setup chroot using shared utility
        self.setup_chroot(self.chroot_path, self.cache_path)
    
    def check_arch_in_pkgbuild(self, pkg_dir):
        """Check if PKGBUILD arch array contains target architecture using bash sourcing"""
        try:
            result = subprocess.run(
                ['bash', '-c', f'cd {pkg_dir} && source PKGBUILD && printf "%s\\n" "${{arch[@]}}"'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                arch_values = result.stdout.strip().split('\n')
                return self.target_arch in arch_values
        except Exception:
            pass
        return False
    
    def clear_cache(self):
        """Clear all packages from cache directory (uses sudo for root-owned files)"""
        if self.dry_run:
            self.format_dry_run("Would clear pacman cache", [f"sudo rm -rf {self.cache_path}/*"])
            return 0
        
        try:
            # Use sudo rm -rf since chroot operations create root-owned files
            # find -delete can fail on busy files, rm -rf is more reliable
            subprocess.run(
                ["sudo", "rm", "-rf", "--one-file-system", str(self.cache_path)],
                check=True
            )
            # Recreate the directory
            self.cache_path.mkdir(parents=True, exist_ok=True)
            print(f"Cleared cache: {self.cache_path}")
            return 1
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to clear cache: {e}")
            sys.exit(1)  # Cache corruption is fatal for bootstrap
    
    def bootstrap_build_package(self, pkg_name):
        """Build a single package in bootstrap mode"""
        pkg_dir = self.build_dir / pkg_name
        
        if not pkg_dir.exists() and not self.dry_run:
            if pkg_name == "gcc":
                print(f"ERROR: {pkg_dir} not found - this should have been cloned during setup")
                sys.exit(1)
        
        all_toolchain = STAGE1_PACKAGES + ["gcc-libs"]
        target_repo = "extra-testing" if pkg_name == "valgrind" else "core-testing"
        
        if self.dry_run:
            self.format_dry_run(f"Would bootstrap build {pkg_name}", [
                f"arch-nspawn {self.chroot_path}/root pacman -Syu --noconfirm {' '.join(all_toolchain)}",
                f"sudo rm -rf {self.cache_path}",
                f"makechrootpkg -l build-{pkg_name} -r {self.chroot_path} -d {self.cache_path} -c -u -- --ignorearch",
                f"repo-upload *.pkg.tar.zst --arch {self.target_arch} --repo {target_repo}",
                f"sudo rm -rf {self.cache_path}"
            ])
            return True
        
        print(f"Bootstrap building {pkg_name}...")
        
        # Update chroot and install toolchain dependencies
        try:
            self.run_command([
                "arch-nspawn", str(self.chroot_path / "root"),
                "pacman", "-Syu", "--noconfirm"
            ] + all_toolchain)
        except subprocess.CalledProcessError:
            # Clear cache on pacman failure to remove any corrupted packages
            print("Pacman failed, clearing toolchain packages from cache...")
            removed_count = self.clear_packages_from_cache(self.cache_path, all_toolchain)
            print(f"Cleared {removed_count} toolchain packages from cache after pacman failure")
        
        # Clear pacman cache before build
        self.clear_cache()
        
        # Build package
        try:
            env = os.environ.copy()
            env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
            
            use_ignorearch = not self.check_arch_in_pkgbuild(pkg_dir)
            
            cmd = [
                "makechrootpkg",
                "-l", f"build-{pkg_name}",
                "-r", str(self.chroot_path),
                "-d", str(self.cache_path),
                "-c", "-u",
                "-t", "/tmp:size=128G",
                "--"
            ]
            if use_ignorearch:
                cmd.append("--ignorearch")
            
            process = subprocess.Popen(cmd, cwd=pkg_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                      text=True, bufsize=1, errors='replace', env=env)
            
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
            uploaded_count = upload_packages(pkg_dir, target_repo, self.dry_run)
            print(f"Successfully uploaded {uploaded_count} packages to {target_repo}")
            
            # Clear cache after successful build to force using newly uploaded packages
            self.clear_cache()
            
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to bootstrap build {pkg_name}: {e}")
            sys.exit(1)
    
    def build_stage(self, stage_name, packages, stage_num, total_stages):
        """Build a stage of packages"""
        print(f"\n=== {stage_name} ({stage_num}/{total_stages}) ===")
        
        start_index = self.get_start_index(packages) if stage_num == 1 else 0
        
        if start_index > 0:
            print(f"Starting from package {start_index + 1}/{len(packages)} in {stage_name}")
        
        built_count = 0
        for i, pkg_name in enumerate(packages):
            if i < start_index:
                print(f"Skipping {pkg_name} ({i + 1}/{len(packages)}) - start_from specified")
                continue
                
            print(f"\n--- Building {pkg_name} ({i + 1}/{len(packages)}) ---")
            success = self.bootstrap_build_package(pkg_name)
            if not success:
                print(f"ERROR: Failed to build {pkg_name}")
                sys.exit(1)
            built_count += 1
            self.save_progress(i)
            print(f"✓ {pkg_name} built successfully")
        
        print(f"✓ {stage_name} completed: {built_count}/{len(packages) - start_index} packages built")
        return built_count

    def clone_package(self, pkg_name):
        """Clone or update a package repository"""
        pkg_dir = self.build_dir / pkg_name
        
        if pkg_name == "gcc":
            # Special repo - clone if missing, preserve if exists
            if pkg_dir.exists():
                if self.dry_run:
                    self.format_dry_run(f"Would preserve existing {pkg_name} (special repo)", [])
                else:
                    print(f"✓ Preserving existing {pkg_name} (special repo - not overwriting changes)")
                return
            
            if self.dry_run:
                self.format_dry_run(f"Would clone {pkg_name} from special repo", [f"git clone -b {GCC_BRANCH} {GCC_REPO_URL} {pkg_name}"])
            else:
                try:
                    self.run_command(["git", "clone", "-b", GCC_BRANCH, GCC_REPO_URL, pkg_name], cwd=self.build_dir)
                    print(f"✓ Cloned {pkg_name} from {GCC_REPO_URL} (branch: {GCC_BRANCH})")
                except subprocess.CalledProcessError as e:
                    print(f"ERROR: Failed to clone {pkg_name}: {e}")
                    sys.exit(1)
        else:
            # Regular Arch packages - use pkgctl
            if pkg_dir.exists():
                if self.dry_run:
                    self.format_dry_run(f"Would update {pkg_name}", [f"pkgctl repo clone {pkg_name} (updates existing)"])
                else:
                    print(f"✓ Using existing {pkg_name}")
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

    def run_bootstrap(self):
        """Run staged bootstrap toolchain build"""
        print("=== Staged Bootstrap Toolchain Build ===")
        print(f"Target architecture: {self.target_arch}")
        print(f"Stage 1: {len(STAGE1_PACKAGES)} packages")
        print(f"Stage 2: {len(STAGE2_PACKAGES)} packages")
        
        # Lock file in current directory (not BUILD_ROOT which gets cleared)
        lock_file = Path("bootstrap.lock")
        
        try:
            if not self.dry_run:
                if lock_file.exists():
                    try:
                        lock_content = lock_file.read_text().strip()
                        if lock_content.startswith("PID:"):
                            old_pid = int(lock_content.split(":")[1].split("\n")[0])
                            try:
                                os.kill(old_pid, 0)
                                print("ERROR: Bootstrap already running (bootstrap.lock exists)")
                                print("If no other bootstrap is running, remove bootstrap.lock and try again")
                                sys.exit(1)
                            except OSError:
                                lock_file.unlink()
                                print("Removed stale bootstrap.lock file")
                    except (ValueError, IndexError):
                        lock_file.unlink()
                        print("Removed invalid bootstrap.lock file")
                
                current_pid = os.getpid()
                lock_file.write_text(f"PID:{current_pid}\nBootstrap started at {subprocess.run(['date'], capture_output=True, text=True).stdout.strip()}")
        except FileExistsError:
            print("ERROR: Bootstrap already running (bootstrap.lock exists)")
            sys.exit(1)
        
        def signal_handler(signum, frame):
            print(f"\nReceived signal {signum}, cleaning up...")
            if not self.dry_run and lock_file.exists():
                lock_file.unlink()
            sys.exit(1)
        
        if not self.dry_run:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Validate required tools
            for tool in REQUIRED_TOOLS:
                if not self.dry_run and not shutil.which(tool):
                    print(f"ERROR: Required tool '{tool}' not found in PATH")
                    sys.exit(1)
            
            # Clear build directory for fresh start (unless continuing)
            if not self.continue_build and not self.start_from:
                if self.dry_run:
                    self.format_dry_run("Would clear build directory for fresh start", [f"rm -rf {BUILD_ROOT}"])
                else:
                    print(f"Clearing build directory for fresh bootstrap: {BUILD_ROOT}")
                    try:
                        if Path(BUILD_ROOT).exists():
                            subprocess.run(["sudo", "rm", "-rf", BUILD_ROOT], check=True)
                        Path(BUILD_ROOT).mkdir(parents=True, exist_ok=True)
                        print("Build directory cleared and recreated")
                    except subprocess.CalledProcessError as e:
                        print(f"ERROR: Failed to clear build directory: {e}")
                        sys.exit(1)
            
            # Clone all toolchain PKGBUILDs
            print("Checking out all toolchain PKGBUILDs...")
            for pkg_name in ALL_BOOTSTRAP_PACKAGES:
                self.clone_package(pkg_name)
            
            self.setup_environment()
            
            # Validate chroot environment
            if not self.dry_run:
                chroot_root = self.chroot_path / "root"
                if not chroot_root.exists():
                    print("ERROR: Chroot root directory not found - environment setup failed")
                    sys.exit(1)
                if not (chroot_root / "usr" / "bin" / "pacman").exists():
                    print("ERROR: Chroot pacman not found - chroot environment is invalid")
                    sys.exit(1)
            
            # Clear cache before starting
            self.clear_cache()
            
            # Build stages
            total_built = 0
            
            stage1_built = self.build_stage("Stage 1 - Initial Build", STAGE1_PACKAGES, 1, 2)
            if stage1_built == 0 and len(STAGE1_PACKAGES) > 0:
                print("ERROR: Stage 1 failed - no packages were built")
                sys.exit(1)
            total_built += stage1_built
            
            stage2_built = self.build_stage("Stage 2 - Final Rebuild", STAGE2_PACKAGES, 2, 2)
            if stage2_built == 0 and len(STAGE2_PACKAGES) > 0:
                print("ERROR: Stage 2 failed - no packages were built")
                sys.exit(1)
            total_built += stage2_built
            
            print(f"\n=== Bootstrap Summary ===")
            print(f"Successfully built: {total_built} packages total")
            print("Staged toolchain bootstrap completed successfully!")
            
            # Clean up progress file on success
            if not self.dry_run and self.progress_file.exists():
                self.progress_file.unlink()
            
        finally:
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
    parser.add_argument('--start-from', metavar='PACKAGE', choices=ALL_BOOTSTRAP_PACKAGES,
                       help=f'Start from specific package ({", ".join(ALL_BOOTSTRAP_PACKAGES)})')
    parser.add_argument('--one-shot', metavar='PACKAGE', choices=ALL_BOOTSTRAP_PACKAGES,
                       help='Build only the specified package once')
    
    args = parser.parse_args()
    
    if args.one_shot:
        builder = BootstrapBuilder(chroot_path=args.chroot, cache_path=args.cache, dry_run=args.dry_run)
        builder.setup_environment()
        builder.clone_package(args.one_shot)
        
        success = builder.bootstrap_build_package(args.one_shot)
        if success:
            print(f"✓ Successfully built {args.one_shot}")
        else:
            print(f"ERROR: Failed to build {args.one_shot}")
            sys.exit(1)
        return
    
    builder = BootstrapBuilder(chroot_path=args.chroot, cache_path=args.cache, 
                              dry_run=args.dry_run, continue_build=args.continue_build,
                              start_from=args.start_from)
    builder.run_bootstrap()


if __name__ == "__main__":
    main()
