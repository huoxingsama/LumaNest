"""Explicit entry point for the supported, isolated media workers."""
import argparse
import runpy
import sys
from pathlib import Path

WORKERS = ('metadata_proxy', 'cache_worker', 'person_portrait_worker',
           'metadata_matcher', 'chinese_credits_worker', 'sync_external_ratings')


def prepare():
    legacy = Path(__file__).parent / 'legacy'
    if str(legacy) not in sys.path:
        sys.path.insert(0, str(legacy))
    return legacy


def main(argv=None):
    parser = argparse.ArgumentParser(description='Personal Cinema managed worker')
    parser.add_argument('worker', choices=WORKERS)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    prepare()
    sys.argv = [args.worker, *args.arguments]
    runpy.run_module(args.worker, run_name='__main__')


if __name__ == '__main__':
    main()
