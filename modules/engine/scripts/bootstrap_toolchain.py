#!/usr/bin/env python3
"""Install the pinned Silk release and Silk's Clang on a machine that has neither.

`silk task` needs Silk, so this is a script, not a task:

  python3 modules/engine/scripts/bootstrap_toolchain.py
  python3 modules/engine/scripts/bootstrap_toolchain.py --silk-archive silk.tgz --llvm-archive silk-llvm.tar.gz

Silk: the release the workspace's silk.toml pins, through the installer of silk-lang/silk, or from
--silk-archive (a silk-<version>-<target>.tar.gz release asset) where the GitHub release API is not
reachable. It lands in $SILK_HOME (default ~/.silk); put $SILK_HOME/bin on PATH.

Clang: from --llvm-archive or $SILK_LLVM_ARCHIVE, a silk-llvm-<version>-<target>.tar.gz of Silk's
LLVM release (one multicall binary), unpacked to --llvm-prefix (default $SILK_HOME/llvm) with the
tool names linked to it. Without an archive, a Clang 22 or newer already on the machine is used.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

REPOSITORY = Path(__file__).resolve().parents[3]
INSTALLER = 'https://raw.githubusercontent.com/silk-lang/silk/main/install.sh'
LLVM_TOOLS = ['clang', 'clang++', 'ld.lld', 'lld', 'llvm-ar', 'llvm-nm', 'llvm-objcopy', 'llvm-strip', 'llvm-ranlib']


def pinned_version() -> str:
    return tomllib.loads((REPOSITORY / 'silk.toml').read_text())['workspace']['silk-version']


def run(argv, **options):
    print('$ ' + ' '.join(str(word) for word in argv), flush=True)
    subprocess.run([str(word) for word in argv], check=True, **options)


def install_silk(home: Path, version: str, archive: str | None):
    toolchain = home / 'toolchains' / version
    if (toolchain / 'bin/silk').is_file() and (home / 'bin/silk').is_file():
        print(f'Silk {version} is already installed in {toolchain}')
        return
    if archive:
        if not Path(archive).is_file():
            raise SystemExit(f'--silk-archive: {archive} does not exist')
        toolchain.mkdir(parents=True, exist_ok=True)
        (home / 'bin').mkdir(parents=True, exist_ok=True)
        run(['tar', 'xzf', archive, '-C', toolchain, '--strip-components', '1'])
        shutil.copy2(toolchain / 'bin/silk', home / 'bin/silk')
        (home / 'settings.toml').write_text(f'default = "{version}"\n')
        return
    environment = dict(os.environ, SILK_HOME=str(home), SILK_RELEASE='v' + version)
    with tempfile.TemporaryDirectory() as scratch:
        script = Path(scratch) / 'install.sh'
        try:
            run(['curl', '-fsSL', '--retry', '3', '-o', script, INSTALLER])
            run(['sh', script], env=environment)
        except subprocess.CalledProcessError:
            raise SystemExit(
                f'The Silk installer could not install v{version}. Where the GitHub release API is blocked, '
                f'download silk-{version}-<target>.tar.gz from the v{version} release of silk-lang/silk '
                'another way and pass it with --silk-archive.')


def install_llvm(home: Path, archive: str | None, prefix: str | None):
    archive = archive or os.environ.get('SILK_LLVM_ARCHIVE')
    if not archive:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from toolchain import clang_path
        clang = clang_path()  # exits with what to install when there is none
        print(f'No --llvm-archive: using {clang}. Set SILK_CLANG to choose another Clang.')
        return
    if not Path(archive).is_file():
        raise SystemExit(f'--llvm-archive: {archive} does not exist')
    target = Path(prefix).expanduser().resolve() if prefix else home / 'llvm'
    target.mkdir(parents=True, exist_ok=True)
    run(['tar', 'xzf', archive, '-C', target, '--strip-components', '1'])
    multicall = target / 'bin/llvm'
    if not multicall.is_file():
        raise SystemExit(f'{archive} has no bin/llvm: it is not a Silk LLVM release archive')
    for name in LLVM_TOOLS:
        link = target / 'bin' / name
        if link.is_symlink() or not link.exists():
            link.unlink(missing_ok=True)
            link.symlink_to('llvm')
    print(f'Clang is in {target}/bin. For the builds:\n  export SILK_CLANG={target}/bin/clang PATH={target}/bin:$PATH')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--silk-archive', help='a silk-<version>-<target>.tar.gz release asset to install from')
    parser.add_argument('--llvm-archive', help="Silk's LLVM release archive (default $SILK_LLVM_ARCHIVE)")
    parser.add_argument('--llvm-prefix', help='where to unpack it (default $SILK_HOME/llvm)')
    args = parser.parse_args()
    home = Path(os.environ.get('SILK_HOME') or '~/.silk').expanduser().resolve()
    version = pinned_version()
    install_silk(home, version, args.silk_archive)
    install_llvm(home, args.llvm_archive, args.llvm_prefix)
    print(f'Ready: Silk {version}. Put {home}/bin on PATH, then `silk task` in modules/engine or modules/runtime.')


if __name__ == '__main__':
    main()
