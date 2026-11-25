"""Select caption provenance and reuse the originating download's HTTP session."""
import json
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree


def empty_caption(path):
    """Recognize empty caption payloads without treating malformed files as missing."""
    content = path.read_bytes()
    try:
        if path.suffix == '.vtt':
            from yt_dlp.webvtt import CueBlock, ParseError, parse_fragment
            try:
                return not any(isinstance(block, CueBlock) for block in parse_fragment(content))
            except ParseError:
                return not content.decode('utf-8-sig').strip()
        if path.suffix == '.srt':
            return not content.decode('utf-8-sig').strip()
        if path.suffix == '.json3':
            document = json.loads(content)
            if not isinstance(document, dict) or document.get('wireMagic') != 'pb3':
                return False
            events = document.get('events', [])
            return isinstance(events, list) and all(isinstance(event, dict) and 'segs' not in event for event in events)
        if path.suffix in ('.ttml', '.srv3'):
            xml = ElementTree.fromstring(content)
            root, cue = ('tt', 'p') if path.suffix == '.ttml' else ('timedtext', 'p')
            return xml.tag.rsplit('}', 1)[-1] == root and not any(
                node.tag.rsplit('}', 1)[-1] in (cue, 'text') for node in xml.iter())
    except (ValueError, ElementTree.ParseError):
        pass
    return False


def translated(track):
    url = urlsplit(track.get('url') or '')
    if url.hostname not in ('www.youtube.com', 'youtube.com', 'm.youtube.com'):
        return False
    query = parse_qs(url.query)
    target = query.get('tlang', [''])[0]
    return bool(target and target != query.get('lang', [''])[0])


def caption_entries(language, tracks):
    """yt-dlp also lists live chat as subtitles; it is not a caption track."""
    if language == 'live_chat':
        return []
    return [track for track in tracks or []
            if track.get('protocol') not in ('youtube_live_chat', 'youtube_live_chat_replay')]


def automatic_tracks(captions, language):
    # Keep the requested delivery language; -orig is an extractor alias only.
    original = [track for track in caption_entries(language, captions.get(language + '-orig'))
                if not translated(track)]
    exact = caption_entries(language, captions.get(language))
    native = [track for track in exact if not translated(track)]
    return original or native or exact


def select_tracks(info, languages, sources):
    available = {}
    for language in languages:
        manual = (caption_entries(language, (info.get('subtitles') or {}).get(language)) or None
                  if 'manual' in sources else None)
        automatic = automatic_tracks(info.get('automatic_captions') or {}, language) if 'automatic' in sources else None
        if manual or automatic:
            available[language] = (manual, automatic)
    return available


def original_tracks(info, sources):
    """Discover real caption languages without constructing translated requests."""
    available = {}
    if 'manual' in sources:
        for language, tracks in (info.get('subtitles') or {}).items():
            native = [track for track in caption_entries(language, tracks) if not translated(track)]
            if native:
                available[language] = (native, None)
    if 'automatic' in sources:
        captions = info.get('automatic_captions') or {}
        for key, tracks in captions.items():
            language = key.removesuffix('-orig')
            # Translations of manual tracks can use synthetic keys such as en-en.
            native = []
            for track in caption_entries(language, tracks):
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
            translated_tracks = [track for track in caption_entries(key, tracks) if translated(track)]
            if translated_tracks:
                available[language] = (translated_tracks, None)
                break
    return available


def main_language(info):
    """Use source audio metadata, never guess from the first caption language."""
    formats = [f for f in info.get('formats') or [] if f.get('acodec') != 'none'
               and f.get('language') and f['language'] != 'und']
    originals = [f for f in formats if (f.get('language_preference') or 0) >= 10]
    languages = {f['language'].removesuffix('-orig') for f in originals}
    if len(languages) == 1:
        return languages.pop()
    if info.get('language') and info['language'] != 'und':
        return info['language'].removesuffix('-orig')
    languages = {f['language'].removesuffix('-orig') for f in formats}
    if len(languages) == 1:
        return languages.pop()
    languages = {key.removesuffix('-orig') for key, tracks in (info.get('automatic_captions') or {}).items()
                 if key.endswith('-orig') and caption_entries(key, tracks)
                 and any(not translated(t) for t in tracks)}
    return languages.pop() if len(languages) == 1 else None


def scoped_originals(info, config):
    available = original_tracks(info, config['sources'])
    if config['scope'] == 'all_original':
        return available, list(available)
    if config['scope'] == 'selected':
        requested = config['original_languages']
    else:
        language = main_language(info)
        if not language:
            return {}, ['']  # Explicit missing requirement; never broaden or silently succeed.
        if language not in available:
            matches = [key for key in available if key.split('-')[0] == language.split('-')[0]]
            if len(matches) == 1:
                language = matches[0]
        requested = [language]
    return {key: available[key] for key in requested if key in available}, list(requested)


def caption_plan(info, config):
    mode = config['mode']
    available, original = scoped_originals(info, config)
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
        # The originating downloader owns the cookie jar, connection pool,
        # progress hooks and error handling for the entire acquisition.
        self.downloader = downloader

    def download(self, info, language, tracks, directory):
        """Write one caption without reprocessing video formats or extractors.

        Use yt-dlp's caption selector/writer so format preference, inline data,
        existing files, headers and network errors keep their native semantics.
        File-start pacing still runs through the same dl(subtitle=True) path.
        """
        downloader = self.downloader
        params = downloader.params
        downloader.params = {**params, 'writesubtitles': True, 'writeautomaticsub': True,
            'subtitleslangs': [language], 'subtitlesformat': 'vtt/best',
            'outtmpl': {'default': str(directory / 'caption.%(ext)s')},
            'max_filesize': 8 * 1024 * 1024}
        try:
            manual, automatic = tracks
            selected = downloader.process_subtitles(info['id'],
                {language: manual} if manual else {},
                {language: automatic} if not manual and automatic else {})
            # A tiny caption-only information object avoids copying the format
            # list and prevents video selection/postprocessors from running.
            caption = {key: info[key] for key in ('id', 'title', 'http_headers') if key in info}
            caption.update(ext='vtt', requested_subtitles={k: dict(v) for k, v in (selected or {}).items()})
            written = downloader._write_subtitles(caption, str(directory / 'caption.vtt'))
            return [path for path, _ in written or []]
        finally:
            downloader.params = params


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
