"""Worker command-line lifecycle and process diagnostics."""
import argparse
import os
from pathlib import Path
from webapp.diagnostics import event, error_details, process_diagnostics


def main(drain):
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--role', choices=('all', 'regular', 'download', 'maintenance', 'subtitles'), default='all')
    args = parser.parse_args()
    with process_diagnostics(args.state, args.slot):
        event('worker_boot', worker_slot=args.slot, role=args.role, pid=os.getpid())
        try:
            drain(args.state, args.slot, args.role)
        except BaseException as error:
            event('worker_crash', worker_slot=args.slot, **error_details(error))
            raise
        else:
            event('worker_stopped', worker_slot=args.slot)
