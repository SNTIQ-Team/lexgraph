"""Stage immutable releases with checksum-selected hardlinks and a disk reserve."""
import argparse
from pathlib import Path
import shutil
import subprocess

def stage(source: Path, target: Path, previous: Path | None, reserve_bytes: int):
    if not source.is_dir() or source.is_symlink() or reserve_bytes < 0:
        raise ValueError('invalid staging source/reserve')
    if not target.is_dir() or target.is_symlink() or any(target.iterdir()):
        raise ValueError('target must be an empty real directory')
    total = 0
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('release source contains a symlink')
        if path.is_file():
            total += path.stat().st_size
    # Conservative full-copy requirement; sharing may save more but never
    # justifies betting the API/mail server's reserve on an estimate.
    if shutil.disk_usage(target).free < total + reserve_bytes:
        raise OSError('insufficient free space for publication and reserve')
    command = ['rsync', '-a', '--checksum', '--no-times', '--no-owner', '--no-group', '--no-perms', '--delete']
    if previous is not None:
        previous = previous.resolve(strict=True)
        if previous.parent != target.parent.resolve() or previous == target.resolve() or not previous.name.startswith('web-data.release-'):
            raise ValueError('previous release must be an immutable sibling generation')
        command.append('--link-dest=' + str(previous))
    # Never --inplace: the previous published inode is read-only history.
    subprocess.run(command + [str(source.resolve())+'/',str(target.resolve())+'/'],check=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('target',type=Path)
    parser.add_argument('--previous',type=Path);parser.add_argument('--reserve-mib',type=int,default=2048)
    args=parser.parse_args();stage(args.source,args.target,args.previous,args.reserve_mib*1024*1024)

if __name__=='__main__':main()
