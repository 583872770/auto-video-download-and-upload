"""Export durable video facts without cookies, headers or temporary stream URLs."""
import math


TEXT_FIELDS = ('id', 'title', 'description', 'channel', 'channel_id', 'uploader',
               'uploader_id', 'upload_date', 'language', 'license', 'availability')
NUMBER_FIELDS = ('duration', 'timestamp', 'view_count', 'like_count', 'comment_count')


def video_metadata(info):
    result = {'schema_version': 1}
    for key in TEXT_FIELDS:
        if isinstance(info.get(key), str):
            result[key] = info[key]
    for key in NUMBER_FIELDS:
        value = info.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = value
    for key in ('tags', 'categories'):
        if isinstance(info.get(key), list):
            result[key] = [value for value in info[key] if isinstance(value, str)]
    if isinstance(info.get('chapters'), list):
        result['chapters'] = []
        for chapter in info['chapters']:
            if not isinstance(chapter, dict):
                continue
            clean = {}
            if isinstance(chapter.get('title'), str):
                clean['title'] = chapter['title']
            for key in ('start_time', 'end_time'):
                value = chapter.get(key)
                if type(value) in (int, float) and math.isfinite(value):
                    clean[key] = value
            result['chapters'].append(clean)
    return result
