"""Media suffixes and yt-dlp format-track naming shared by indexing and cleanup."""
import re

VIDEO_SUFFIXES = {'.mp4', '.mkv', '.webm', '.mov'}
IMAGE_SUFFIXES = ('.webp', '.jpg', '.jpeg', '.png')
FORMAT_TRACK = re.compile(r'^(?P<stem>.+)\.f[\w-]+$')
