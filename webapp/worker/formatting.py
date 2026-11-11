"""Human-readable worker log values."""


def format_bytes(value):
    """Format byte counts for the human-readable execution log."""
    try:
        amount = max(0, int(value or 0))
    except (TypeError, ValueError):
        amount = 0
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    size = float(amount)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024
