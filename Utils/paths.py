"""Windows extended paths for long media filenames, without altering short paths."""
import os


def io_path(value):
    value = os.fspath(value)
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return value
    absolute = os.path.abspath(value)
    if len(absolute) < 240:
        return value
    if absolute.startswith('\\\\'):
        return '\\\\?\\UNC\\' + absolute[2:]
    return '\\\\?\\' + absolute
