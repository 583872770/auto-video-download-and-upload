"""Select caption provenance and reuse the originating download's HTTP session."""
from urllib.parse import parse_qs, urlsplit


def translated(track):
    url = urlsplit(track.get('url') or '')
    if url.hostname not in ('www.youtube.com', 'youtube.com', 'm.youtube.com'):
        return False
    query = parse_qs(url.query)
    target = query.get('tlang', [''])[0]
    return bool(target and target != query.get('lang', [''])[0])


def automatic_tracks(captions, language):
    # Keep the requested delivery language; -orig is an extractor alias only.
    original = [track for track in captions.get(language + '-orig', []) if not translated(track)]
    exact = captions.get(language) or []
    native = [track for track in exact if not translated(track)]
    return original or native or exact


def select_tracks(info, languages, sources):
    available = {}
    for language in languages:
        manual = (info.get('subtitles') or {}).get(language) if 'manual' in sources else None
        automatic = automatic_tracks(info.get('automatic_captions') or {}, language) if 'automatic' in sources else None
        if manual or automatic:
            available[language] = (manual, automatic)
    return available


def original_tracks(info, sources):
    """Discover real caption languages without constructing translated requests."""
    available = {}
    if 'manual' in sources:
        for language, tracks in (info.get('subtitles') or {}).items():
            native = [track for track in tracks if not translated(track)]
            if native:
                available[language] = (native, None)
    if 'automatic' in sources:
        captions = info.get('automatic_captions') or {}
        for key, tracks in captions.items():
            language = key.removesuffix('-orig')
            # Translations of manual tracks can use synthetic keys such as en-en.
            native = []
            for track in tracks:
                source = parse_qs(urlsplit(track.get('url') or '').query).get('lang', [language])[0]
                if not translated(track) and source == language:
                    native.append(track)
            if native and (language not in available or not available[language][0]):
                # Prefer the explicit original alias regardless of extractor order.
                if language not in available or key.endswith('-orig'):
                    available[language] = (None, native)
    return available


def target_tracks(info, languages):
    available = select_tracks(info, languages, ['manual', 'automatic'])
    for language in languages:
        if language in available:
            continue
        # yt-dlp labels translations of manual captions <target>-<source>.
        for key, tracks in (info.get('subtitles') or {}).items():
            if not key.startswith(language + '-'):
                continue
            translated_tracks = [track for track in tracks if translated(track)]
            if translated_tracks:
                available[language] = (translated_tracks, None)
                break
    return available


def caption_plan(info, config):
    mode = config.get('mode', 'selected')
    if mode == 'selected':
        available = select_tracks(info, config['languages'], config['sources'])
        required = config['languages'] if config['language_policy'] == 'all_selected' else list(available)
        return available, required
    available = original_tracks(info, config['sources'])
    original = list(available)
    if mode == 'original_and_translated':
        targets = target_tracks(info, config['languages'])
        for language, tracks in targets.items():
            available.setdefault(language, tracks)
        required = list(dict.fromkeys(original + (config['languages'] if config['language_policy'] == 'all_selected' or not targets else list(targets))))
    else:
        required = original
    return available, required


class SubtitleSession:
    def __init__(self, downloader):
        self.cookiejar = downloader.cookiejar

    def attach(self, downloader):
        # Set the cached property before creating any network request handler.
        # Verbose initialization may already have built handlers for its header.
        director = downloader.__dict__.pop('_request_director', None)
        if director is not None:
            director.close()
        downloader.cookiejar = self.cookiejar


def provider_options(options, *, force_subs=False):
    """Optional instance deployment setting; tokens are minted for this session."""
    import os
    endpoint = os.environ.get('VIDEO_SUBTITLE_POT_URL', '').strip()
    if not endpoint:
        return options
    args = {key: dict(value) for key, value in options.get('extractor_args', {}).items()}
    args['youtubepot-bgutilhttp'] = {**args.get('youtubepot-bgutilhttp', {}), 'base_url': [endpoint]}
    if force_subs:
        args['youtube'] = {**args.get('youtube', {}), 'fetch_pot': ['always']}
    return {**options, 'extractor_args': args}
