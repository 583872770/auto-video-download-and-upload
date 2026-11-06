"""One failure vocabulary and recovery policy for download admission layers.

HTTP evidence is never manufactured from a policy category. Records are bounded,
sanitized and carry one incident ID across exception wrappers and scheduler layers.
"""
from dataclasses import asdict, dataclass, replace
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import math
import re
import time
import uuid

from yt_dlp.networking.exceptions import IncompleteRead as YtdlpIncompleteRead

from webapp.diagnostics import safe_text


def retry_after(value, now=None):
    now = time.time() if now is None else now
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return min(86400, max(1, seconds)) if math.isfinite(seconds) and seconds >= 0 else None


@dataclass(frozen=True)
class FailureEvidence:
    incident_id: str
    observed_at: float
    source: str
    phase: str
    kind: str
    http_status: int | None
    evidence: str
    retry_after: float | None
    retryable: bool
    message: str
    task_id: str | None = None
    profile_id: str | None = None
    profile_name: str | None = None

    def public(self):
        return asdict(self)


def exception_chain(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def failure_evidence(error, *, phase=None, source=None, task_id=None, profile_id=None, profile_name=None):
    chain = list(exception_chain(error))
    for cause in chain:
        saved = getattr(cause, '_download_evidence', None)
        if isinstance(saved, FailureEvidence):
            saved = replace(saved, phase=phase if saved.phase == 'unknown' and phase else saved.phase,
                            source=source if saved.source == 'unknown' and source else saved.source,
                            task_id=saved.task_id or task_id, profile_id=saved.profile_id or profile_id,
                            profile_name=saved.profile_name or (safe_text(profile_name)[:80] if profile_name else None))
            for member in chain:
                _attach(member, saved)
            return saved
    # URLs and secrets must not take part in classification, either.
    text = '\n'.join(safe_text(str(cause))[:2000] for cause in chain)
    text = re.sub(r'https?://[^\s<>"\']+', '[url]', text)
    lower = text.lower()
    status, evidence = None, 'message'
    delay = None
    for cause in chain:
        response = getattr(cause, 'response', None)
        candidate = (getattr(cause, 'http_status', None) or getattr(cause, 'status', None)
                     or getattr(response, 'status', None) or getattr(response, 'status_code', None)
                     or getattr(cause, 'code', None))
        if type(candidate) is int and 100 <= candidate <= 599 and status is None:
            status, evidence = candidate, 'http_response'
        headers = getattr(cause, 'headers', None) or getattr(response, 'headers', None) or {}
        value = retry_after(getattr(cause, 'retry_after', None) or headers.get('Retry-After') or headers.get('retry-after'))
        delay = max(delay or 0, value) if value else delay
    if status is None:
        match = re.search(r'\bHTTP(?:\s+Error)?[\s:]+([45]\d\d)\b', text, re.I)
        if match:
            status, evidence = int(match[1]), 'http_message'
    result = next((getattr(c, 'result') for c in chain
                   if type(c).__name__ == 'YouTubeAuthRequired'), {})
    if result or any(getattr(c, 'account_rate_limited', False) for c in chain):
        source = source or 'youtube'
    local_types = {'PipelineBusy', 'DownloadLeaseLost', 'DiskPressure', 'DiskCoordinationBusy',
                   'BrowserEnvironmentRequired', 'DownloadEnvironmentRequired'}
    local = any(any(cls.__name__ in local_types for cls in type(c).__mro__) or type(c).__module__.startswith('sqlalchemy') or
                isinstance(c, OSError) and c.errno in (5, 13, 28, 30, 122) for c in chain)
    transient = any(bool(getattr(c, 'retryable', False)) or isinstance(c, (TimeoutError, ConnectionError))
                    or type(c).__name__ == 'WebsiteTimeoutError' for c in chain)
    # yt-dlp may flatten IncompleteRead into its terminal error message before
    # returning a failure code. Match its byte-count diagnostic as well as the
    # original exception; a partial response is not a permanent content error.
    incomplete_read = any(isinstance(c, (IncompleteRead, YtdlpIncompleteRead)) for c in chain) or bool(re.search(
        r'\b\d+ bytes read,\s*\d+ more expected\b', lower))
    if any(type(c).__name__ == 'OSSUploadError' for c in chain):
        kind, source, phase = 'storage_failure', 'oss', 'upload'
    elif result.get('source') == 'cookie_pool_halted':
        kind, source, evidence = 'local_halt', 'local', 'local_state'
    elif local:
        kind, source, evidence = 'local_resource', 'local', 'typed_exception'
    elif any(getattr(c, 'account_rate_limited', False) for c in chain) or 'your account has been rate-limited' in lower:
        kind = 'account_rate_limit'
    elif status == 429:
        kind = 'request_rate_limit'
    elif result and not transient:
        kind = 'authentication_required'
        phase = phase or 'auth'
    elif any(getattr(c, 'failure_scope', None) == 'video' for c in chain) or any(term in lower for term in (
            'private video', 'members-only content', 'not available in your country',
            'requested format is not available', 'drm protected', 'video has been deleted',
            'account associated with this video has been terminated')):
        kind = 'content_restriction'
    elif 'confirm' in lower and 'bot' in lower or 'captcha' in lower:
        kind = 'verification_required'
    elif status in (401, 403):
        kind = 'access_denied'
    elif status in (408, 500, 502, 503, 504) or transient or incomplete_read or any(getattr(c, 'connection_failure', False) for c in chain) or any(term in lower for term in (
            'timed out', 'timeout', 'connection reset', 'connection refused', 'name resolution',
            'connection aborted', 'temporary failure', 'sslerror', 'ssleoferror', 'network is unreachable',
            'remote end closed connection', 'err_connection_', 'err_proxy_connection_', 'err_tunnel_connection_')):
        kind = 'transient_failure'
    else:
        kind = 'unknown'
    retryable = kind in ('request_rate_limit', 'transient_failure') or kind == 'storage_failure' and transient
    value = FailureEvidence(uuid.uuid4().hex, time.time(), source or 'unknown', phase or 'unknown', kind,
                            status, evidence, delay, retryable, safe_text(str(error))[:1000],
                            task_id, profile_id, safe_text(profile_name)[:80] if profile_name else None)
    for cause in chain:
        _attach(cause, value)
    return value


def _attach(error, value):
    try:
        error._download_evidence = value
    except (AttributeError, TypeError):
        pass


@dataclass(frozen=True)
class RecoveryPolicy:
    action: str = 'none'
    delay: float = 0
    manual: bool = False
    targets: tuple[str, ...] = ()


def recovery_policy(failure, config, *, youtube=True):
    if (youtube and config.get('youtube_cookie_pool') and failure.profile_id
            and failure.kind in ('account_rate_limit', 'request_rate_limit')):
        delay = max(3600 if failure.kind == 'account_rate_limit' else
                    int(config.get('youtube_throttle_cooldown', 300)), failure.retry_after or 0)
        return RecoveryPolicy('cooldown', delay, False, ('session',))
    if failure.kind in ('account_rate_limit', 'authentication_required', 'verification_required'):
        targets = ('pool', 'session', 'execution')
        if failure.kind == 'account_rate_limit':
            targets += ('shared', 'pacing')
        return RecoveryPolicy('halt', max(3600, failure.retry_after or 0), True, targets)
    if failure.kind == 'request_rate_limit':
        return RecoveryPolicy('cooldown', max(int(config.get('youtube_throttle_cooldown', 300)) if youtube else 30,
                                               failure.retry_after or 0), targets=('pool', 'session', 'shared', 'pacing'))
    if failure.kind == 'transient_failure':
        return RecoveryPolicy('cooldown', max(60, failure.retry_after or 0),
                              targets=('session', 'pacing', 'shared') if failure.http_status == 503 else ('session', 'pacing'))
    return RecoveryPolicy()


def cooldown_deadline(error, stamp, delay):
    """All layers handling one exception share a deadline; waits are not added."""
    base = getattr(error, '_download_cooldown_base', stamp)
    until = max(getattr(error, '_download_cooldown_until', 0), base + delay)
    try:
        error._download_cooldown_base = base
        error._download_cooldown_until = until
        error._download_recovery_delay = until - base
    except (AttributeError, TypeError):
        pass
    return until


def remember_decision(state, failure, *, layer, scope, action, until=0, manual=False, stamp=None):
    record = dict(incident_id=failure.incident_id, time=time.time() if stamp is None else stamp,
                  layer=layer, scope=scope, action=action, until=until, manual=manual,
                  failure=failure.public())
    history = [r for r in state.get('decisions', [])
               if (r.get('incident_id'), r.get('layer')) != (failure.incident_id, layer)]
    state['decisions'] = [*history[-7:], record]
    state['last_decision'] = record
    return record


def execution_decision(store, db, identity, *, error=None, stamp=None):
    """Persist/clear an executor stop in the same transaction as its control flag."""
    import hashlib
    import json
    from sqlalchemy import select
    from webapp.database import download_resources
    stamp = time.time() if stamp is None else stamp
    key = hashlib.sha256(('execution:' + identity).encode()).hexdigest()
    if error is not None:
        store.db.ensure(db, download_resources, {'key': key, 'state': '{}'})
    raw = db.execute(select(download_resources.c.state).where(download_resources.c.key == key).with_for_update()).scalar_one_or_none()
    if raw is None:
        return
    state = json.loads(raw)
    state['active'] = error is not None
    if error is not None:
        if not isinstance(error, BaseException):
            error = RuntimeError(str(error))
        failure = failure_evidence(error)
        remember_decision(state, failure, layer='execution', scope='当前实例停止领取',
            action='halt', until=getattr(error, '_download_cooldown_until', 0), manual=True, stamp=stamp)
    else:
        state['recovered_at'] = stamp
    db.execute(download_resources.update().where(download_resources.c.key == key).values(state=json.dumps(state)))
