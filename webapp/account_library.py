"""Global YouTube account records; no network requests or Cookie cache writes."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid

from sqlalchemy import func, or_, select, tuple_
from werkzeug.exceptions import Conflict, NotFound

from webapp.account_import import email, secret_fields
from webapp.account_secrets import AccountSecrets
from webapp.database import (cookie_profiles, instances, settings, tasks,
    youtube_accounts as accounts, youtube_account_cookies as links,
    youtube_account_audit as audit, youtube_profile_auth as auth_table, youtube_account_owners as owners)


def context_key(config, generation=''):
    values = [config.get(k, '') for k in ('proxy_mode', 'proxy', 'youtube_source_address')]
    return hashlib.sha256(json.dumps([values, generation], sort_keys=True).encode()).hexdigest()


def auth_setting_key(scope, key, registered=True):
    return f'i:{hashlib.sha256(scope.encode()).hexdigest()[:16]}:{key}' if registered else key


class AccountLibrary:
    def __init__(self, store, base, local_config=None):
        self.store, self.base = store, base
        self.local_config = local_config or {}
        from webapp.cookie_profiles import CookieProfiles
        self.local_id = CookieProfiles.scope_for(base, store)

    @contextmanager
    def transaction(self):
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'fleet_budget').with_for_update()).first()
            # Shared, short metadata lock. No remote operations inside it.
            self.store.db.ensure(db, settings, dict(key='youtube_account_library_lock', value=''))
            db.execute(select(settings.c.key).where(settings.c.key == 'youtube_account_library_lock').with_for_update()).first()
            yield db

    def record(self, db, identity, actor, action):
        db.execute(audit.insert().values(id=uuid.uuid4().hex, account_id=identity,
            actor=actor, action=action, occurred_at=time.time()))

    def get_row(self, db, identity, version=None):
        row = db.execute(select(accounts).where(accounts.c.id == identity).with_for_update()).mappings().first()
        if not row:
            raise NotFound('账号资料不存在。')
        if version is not None and (type(version) is not int or version != row['version']):
            raise Conflict('账号资料已被其他管理端修改，请重新读取后再保存。')
        return dict(row)

    def key(self, db):
        key = AccountSecrets()
        other = db.execute(select(accounts.c.key_id).where(accounts.c.key_id != key.key_id).limit(1)).first()
        if other:
            raise ValueError('当前实例的账号资料密钥与共享资料库不一致，请配置原密钥。')
        return key

    def nodes(self, db):
        rows = db.execute(select(instances.c.id, instances.c.name, instances.c.role,
            instances.c.heartbeat, instances.c.config)).mappings().all()
        result = {}
        stamp = time.time()
        for row in rows:
            if row['role'] not in ('worker', 'hybrid'):
                continue
            local = json.loads(row['config'])
            result[row['id']] = {'id': row['id'], 'name': row['name'],
                'online': row['heartbeat'] > stamp - 45, 'config': local.get('config', {}),
                'throughput': local.get('throughput') or {}}
        if not self.store.fleet:
            result[self.local_id] = {'id': self.local_id, 'name': '本机实例', 'online': True,
                'config': self.local_config, 'throughput': {}}
        return result

    def validate_instance(self, db, identity):
        if identity in ('', None):
            return None
        if not isinstance(identity, str) or identity not in self.nodes(db):
            raise ValueError('请选择已登记的下载实例。')
        return identity

    def owner(self, db, normalized, target, *, claim=False):
        saved = db.execute(select(owners.c.instance_id).where(owners.c.normalized_email == normalized)).scalar_one_or_none()
        if saved and target != saved:
            raise Conflict('此邮箱账号的 Cookie 已归属另一实例；删除或解除资料关联不会释放归属，不能跨实例复用账号。')
        if claim and target and not saved:
            db.execute(owners.insert().values(normalized_email=normalized, instance_id=target))

    @staticmethod
    def metadata(data):
        name = email(data.get('email'))
        note = data.get('note', '')
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError('备注最多 4000 字。')
        return dict(email=name, normalized_email=name.lower(), note=note)

    def create(self, data, actor):
        fields, secrets = self.metadata(data), secret_fields(data, creating=True)
        with self.transaction() as db:
            key = self.key(db)
            if db.execute(select(accounts.c.id).where(accounts.c.normalized_email == fields['normalized_email'])).first():
                raise Conflict('该邮箱账号已存在。')
            identity = uuid.uuid4().hex
            target = self.validate_instance(db, data.get('instance_id'))
            self.owner(db, fields['normalized_email'], target)
            db.execute(accounts.insert().values(id=identity, **fields,
                instance_id=target, version=1,
                secrets_ciphertext=key.encrypt(identity, secrets), key_id=key.key_id,
                created_at=time.time(), updated_at=time.time()))
            self.record(db, identity, actor, 'create')
        return identity

    def update(self, identity, data, actor):
        if type(data.get('version')) is not int:
            raise Conflict('缺少资料版本，请重新读取账号。')
        with self.transaction() as db:
            row = self.get_row(db, identity, data['version'])
            fields = self.metadata({**row, **data})
            duplicate = db.execute(select(accounts.c.id).where(accounts.c.normalized_email == fields['normalized_email'],
                accounts.c.id != identity)).first()
            if duplicate:
                raise Conflict('该邮箱账号已存在。')
            target = self.validate_instance(db, data.get('instance_id', row['instance_id']))
            self.owner(db, fields['normalized_email'], target)
            connected = db.execute(select(links.c.profile_id).where(links.c.account_id == identity).limit(1)).first()
            if connected:
                self.owner(db, fields['normalized_email'], target, claim=True)
            if target != row['instance_id'] and connected:
                raise Conflict('账号已关联 Cookie，不能直接跨实例改绑；解绑资料也不会释放 Cookie 的实际归属。')
            changed = secret_fields(data)
            if changed:
                key = self.key(db)
                secrets = {**key.decrypt(row), **changed}
                fields.update(secrets_ciphertext=key.encrypt(identity, secrets), key_id=key.key_id)
            db.execute(accounts.update().where(accounts.c.id == identity).values(**fields,
                instance_id=target, version=row['version'] + 1, updated_at=time.time()))
            self.record(db, identity, actor, 'update')

    def secrets(self, identity, field, actor):
        if field not in ('password', 'extra', 'code_url'):
            raise ValueError('不支持的密钥字段。')
        with self.transaction() as db:
            row = self.get_row(db, identity)
            value = self.key(db).decrypt(row).get(field, '')
            self.record(db, identity, actor, 'read_' + field)
        return value

    def associate(self, identity, data, actor):
        if type(data.get('version')) is not int or data.get('confirmed') is not True:
            raise ValueError('请确认 Cookie 属于此邮箱账号后再关联。')
        ids = data.get('profile_ids')
        if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('Cookie 关联列表无效。')
        with self.transaction() as db:
            row = self.get_row(db, identity, data['version'])
            target = self.validate_instance(db, data.get('instance_id', row['instance_id']))
            if not target:
                raise ValueError('请先选择绑定实例。')
            self.owner(db, row['normalized_email'], target, claim=bool(ids))
            existing = list(db.execute(select(links).where(links.c.account_id == identity)).mappings())
            if existing and target != row['instance_id']:
                raise Conflict('已有 Cookie 关联的账号不能直接跨实例改绑。')
            profiles = list(db.execute(select(cookie_profiles).where(cookie_profiles.c.scope == target,
                cookie_profiles.c.id.in_(ids), cookie_profiles.c.deleted == 0).with_for_update()).mappings())
            if len(profiles) != len(ids):
                raise Conflict('Cookie 不存在、已删除或不属于该实例。')
            others = db.execute(select(links.c.profile_id).where(links.c.instance_id == target,
                links.c.profile_id.in_(ids), links.c.account_id != identity)).first()
            if others:
                raise Conflict('所选 Cookie 已关联其他账号。')
            from webapp.cookie_binding import claim, credential_keys
            for profile in profiles:
                claim(db, target, credential_keys(profile['content'].encode()))
            db.execute(links.delete().where(links.c.account_id == identity))
            if ids:
                db.execute(links.insert(), [dict(instance_id=target, profile_id=i, account_id=identity,
                    confirmed_by=actor, created_at=time.time()) for i in ids])
            db.execute(accounts.update().where(accounts.c.id == identity).values(instance_id=target,
                version=row['version'] + 1, updated_at=time.time()))
            self.record(db, identity, actor, 'associate')

    def delete(self, identity, version, actor):
        with self.transaction() as db:
            row = self.get_row(db, identity, version)
            bound = list(db.execute(select(links.c.profile_id).where(links.c.account_id == identity)).scalars())
            if bound:
                db.execute(select(instances.c.id).where(instances.c.id == row['instance_id']).with_for_update()).first()
                node = self.nodes(db).get(row['instance_id'], {})
                config = node.get('config', {})
                current = config.get('youtube_cookie', '').replace('\\', '/').rsplit('/', 1)[-1].removesuffix('.txt')
                if current == 'youtube':
                    current = 'legacy'
                if set(bound) & set(config.get('youtube_cookie_pool', [])) or current in bound:
                    raise Conflict('关联 Cookie 仍在账号池或当前使用中，请先停用再删除账号资料。')
                # Conservative fence: no executor tasks may still retain a Cookie snapshot.
                if db.execute(select(tasks.c.id).where(tasks.c.instance_id == row['instance_id'],
                        tasks.c.status == 'running').limit(1)).first():
                    raise Conflict('绑定实例仍有任务执行，请等待当前任务结束后删除资料。')
            db.execute(links.delete().where(links.c.account_id == identity))
            db.execute(accounts.delete().where(accounts.c.id == identity))
            self.record(db, identity, actor, 'delete')

    def inventory(self, db, profiles=None):
        """Batch DB-only state. Never instantiate CookieProfiles or probe YouTube."""
        nodes = self.nodes(db)
        if profiles is None:
            profiles = list(db.execute(select(cookie_profiles).where(cookie_profiles.c.deleted == 0)
                .order_by(cookie_profiles.c.scope, cookie_profiles.c.created_at)).mappings())
        keys = [(p['scope'], p['id']) for p in profiles]
        associations = {(r['instance_id'], r['profile_id']): dict(r) for r in db.execute(
            select(links).where(tuple_(links.c.instance_id, links.c.profile_id).in_(keys))).mappings()} if keys else {}
        saved_auth = {(r['instance_id'], r['profile_id']): r for r in db.execute(
            select(auth_table).where(tuple_(auth_table.c.instance_id, auth_table.c.profile_id).in_(keys))).mappings()} if keys else {}
        generations = dict(db.execute(select(settings.c.key, settings.c.value).where(
            settings.c.key.in_([auth_setting_key(i, 'youtube_auth_generation', bool(self.store.fleet)) for i in nodes]))).all())
        result, stamp = [], time.time()
        for p in profiles:
            node = nodes.get(p['scope'], {})
            config, rate = node.get('config', {}), node.get('throughput', {})
            pool = rate.get('cookie_pool') or {}
            sessions = pool.get('profiles') or pool.get('sessions') or pool.get('session_states') or []
            session = next((s for s in sessions if s['id'] == p['id'] or p['id'] in s.get('profile_ids', [])), None)
            # Alias map in the capacity snapshot maps duplicate files to a core session.
            alias = pool.get('aliases', {}).get(p['id'])
            if session is None and alias:
                session = next((s for s in sessions if s['id'] == alias), None)
            fresh = node.get('online') and 0 <= stamp - rate.get('time', 0) < 20
            pooled = p['id'] in config.get('youtube_cookie_pool', [])
            current = config.get('youtube_cookie', '').replace('\\', '/').rsplit('/', 1)[-1].removesuffix('.txt')
            active = not config.get('youtube_cookie_pool') and p['id'] == ('legacy' if current == 'youtube' else current)
            download = session.get('state', 'unknown') if fresh and session else 'unknown' if pooled or active else 'inactive'
            auth = saved_auth.get((p['scope'], p['id']))
            state, details = 'unchecked', {}
            if auth:
                details = json.loads(auth['auth'])
                generation = json.loads(generations.get(auth_setting_key(p['scope'], 'youtube_auth_generation', bool(self.store.fleet)), '""'))
                matches = (hashlib.sha256(p['content'].encode()).hexdigest() == auth['fingerprint']
                    and context_key(config, generation) == auth['context'])
                state = details.get('state', 'unchecked') if matches else 'unchecked'
                if state == 'valid' and details.get('expires_at', 0) <= stamp:
                    state = 'expired'
                # Automatic proxy rotation has several worker exits. A past
                # success cannot certify the currently selected exit.
                if state == 'valid' and config.get('proxy_mode') == 'clash':
                    state = 'unconfirmed'
                    details = {**details, 'message':'该 Cookie 曾在记录出口验证通过；代理会自动轮换，不能据此确认当前下载出口。'}
                if not matches:
                    details = {'last_valid_at': auth['last_valid_at']}
                else:
                    details = {**details, 'last_valid_at': auth['last_valid_at']}
            result.append(dict(id=p['id'], instance_id=p['scope'], name=p['name'],
                instance_name=node.get('name', p['scope']), online=bool(node.get('online')),
                account_id=(associations.get((p['scope'], p['id'])) or {}).get('account_id'),
                pooled=pooled, active=active, auth={**details, 'state': state}, download_state=download,
                session=session if fresh else None, created_at=p['created_at']))
        return nodes, result

    @staticmethod
    def account_item(row, members, nodes):
        states = {p['auth']['state'] for p in members}
        auth = ('unlinked' if not members else next((s for s in ('valid', 'verification_required', 'not_logged_in',
            'invalid', 'network_error', 'rate_limited', 'unconfirmed', 'expired', 'unchecked') if s in states), 'unchecked'))
        downloads = {p['download_state'] for p in members}
        download = next((s for s in ('manual', 'cooldown', 'recovery', 'ready', 'unknown') if s in downloads), 'inactive')
        node = nodes.get(row['instance_id'], {})
        return {**row, 'kind':'account', 'instance_name':node.get('name', row['instance_id']),
            'online':bool(node.get('online')), 'cookies':members, 'cookie_count':len(members),
            'valid_count':sum(p['auth']['state']=='valid' for p in members),
            'auth_state':auth, 'download_state':download}

    def overview(self, filters):
        """One account row, or one unassociated credential; paginate after grouping.

        All metadata comes from one database snapshot. No credentials are inferred
        to belong to an email and opening the workspace never probes YouTube.
        """
        try:
            page = min(100000, max(1, int(filters.get('page', 1))))
            size = min(100, max(1, int(filters.get('size', 50))))
        except (ValueError, TypeError):
            raise ValueError('分页参数无效。') from None
        search = str(filters.get('q', '')).strip().lower()[:254]
        target = filters.get('instance', '')
        fields = (accounts.c.id, accounts.c.email, accounts.c.note, accounts.c.instance_id,
            accounts.c.version, accounts.c.created_at, accounts.c.updated_at)
        with self.store.connect() as db:
            account_query = select(*fields)
            if target:
                account_query = account_query.where(accounts.c.instance_id.is_(None)
                    if target == 'unbound' else accounts.c.instance_id == target)
            if search:
                matching = select(links.c.account_id).join(cookie_profiles,
                    (links.c.instance_id == cookie_profiles.c.scope) & (links.c.profile_id == cookie_profiles.c.id)
                    ).where(cookie_profiles.c.deleted == 0, func.lower(cookie_profiles.c.name).contains(search, autoescape=True))
                account_query = account_query.where(or_(accounts.c.id.in_(matching),
                    func.lower(accounts.c.email).contains(search, autoescape=True),
                    func.lower(accounts.c.note).contains(search, autoescape=True)))
            rows = list(db.execute(account_query).mappings())
            association = (links.c.instance_id == cookie_profiles.c.scope) & (links.c.profile_id == cookie_profiles.c.id)
            orphan = ~select(links.c.account_id).where(association).exists()
            if target:
                orphan = orphan & (cookie_profiles.c.scope == target) if target != 'unbound' else orphan & False
            if search:
                orphan = orphan & func.lower(cookie_profiles.c.name).contains(search, autoescape=True)
            selected_links = select(links.c.profile_id).where(association, links.c.account_id.in_([r['id'] for r in rows])).exists()
            profiles = list(db.execute(select(cookie_profiles).where(cookie_profiles.c.deleted == 0,
                or_(selected_links, orphan))).mappings())
            nodes, inventory = self.inventory(db, profiles)
        grouped, items = {}, []
        for profile in inventory:
            if profile['account_id']:
                grouped.setdefault(profile['account_id'], []).append(profile)
            else:
                items.append(dict(id='cookie:'+profile['instance_id']+':'+profile['id'], kind='cookie',
                    email=None, note='', instance_id=profile['instance_id'], instance_name=profile['instance_name'],
                    online=profile['online'], created_at=profile['created_at'], cookies=[profile],
                    cookie_count=1, valid_count=int(profile['auth']['state']=='valid'),
                    auth_state=profile['auth']['state'], download_state=profile['download_state']))
        for row in rows:
            members = sorted(grouped.get(row['id'], []), key=lambda p:(p['created_at'], p['id']))
            items.append(self.account_item(dict(row), members, nodes))
        auth, download, linked, pooled = (filters.get(k, '') for k in ('auth', 'download', 'linked', 'pooled'))
        items = [r for r in items if (not auth or r['auth_state'] == auth)
            and (not download or r['download_state'] == download)
            and (linked != 'account' or r['kind'] == 'account')
            and (linked != 'orphan' or r['kind'] == 'cookie')
            and (linked != 'missing' or not r['cookies'])
            and (pooled not in ('0', '1') or any(p['pooled'] for p in r['cookies']) == (pooled == '1'))]
        def created_at(row):
            value = row['created_at']
            if isinstance(value, (int, float)):
                return value
            try:
                stamp = datetime.fromisoformat(value)
                return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
            except (TypeError, ValueError):
                return 0
        items.sort(key=lambda r:(-created_at(r), r['id']))
        total = len(items)
        return {'items':items[(page-1)*size:page*size], 'total':total, 'page':page, 'size':size,
            'local_instance_id':self.local_id,
            'instances':[{k:r[k] for k in ('id','name','online')} for r in nodes.values()]}

    def list(self, filters, *, identity=None, cookies=False):
        def integer(name, default, limit):
            try:
                return min(limit, max(1, int(filters.get(name, default))))
            except (ValueError, TypeError):
                raise ValueError('分页参数无效。') from None
        page, size = integer('page', 1, 100000), integer('size', 50, 100)
        search = str(filters.get('q', '')).strip().lower()[:254]
        target, auth, download = (filters.get(k, '') for k in ('instance', 'auth', 'download'))
        # Simple queries page in SQL; derived runtime status filters require a
        # batch snapshot of matching candidates, never per-account queries.
        sql_paged = not identity and not auth and not download
        fields = (accounts.c.id, accounts.c.email, accounts.c.note, accounts.c.instance_id,
            accounts.c.version, accounts.c.created_at, accounts.c.updated_at)
        with self.store.connect() as db:
            query = select(*fields).order_by(accounts.c.created_at.desc(), accounts.c.id)
            if cookies:
                query = select(cookie_profiles).outerjoin(links,
                    (links.c.instance_id == cookie_profiles.c.scope) & (links.c.profile_id == cookie_profiles.c.id)
                    ).outerjoin(accounts, accounts.c.id == links.c.account_id).where(cookie_profiles.c.deleted == 0
                    ).order_by(cookie_profiles.c.scope, cookie_profiles.c.created_at, cookie_profiles.c.id)
            if identity:
                query = query.where(accounts.c.id == identity)
            if search:
                columns = (accounts.c.email, cookie_profiles.c.name) if cookies else (accounts.c.email, accounts.c.note)
                query = query.where(or_(*(func.lower(c).contains(search, autoescape=True) for c in columns)))
            if target:
                column = cookie_profiles.c.scope if cookies else accounts.c.instance_id
                query = query.where(column.is_(None) if target == 'unbound' else column == target)
            if cookies and filters.get('linked') in ('0', '1'):
                query = query.where(links.c.account_id.is_not(None) if filters['linked'] == '1' else links.c.account_id.is_(None))
            if cookies and filters.get('profile'):
                query = query.where(cookie_profiles.c.id == str(filters['profile']))
            total = db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one() if sql_paged else 0
            if sql_paged:
                query = query.offset((page-1)*size).limit(size)
            selected = list(db.execute(query).mappings())
            if cookies:
                profiles = selected
                ids = select(links.c.account_id).where(tuple_(links.c.instance_id, links.c.profile_id).in_(
                    [(p['scope'],p['id']) for p in profiles]))
                rows = list(db.execute(select(*fields).where(accounts.c.id.in_(ids))).mappings()) if profiles else []
            else:
                rows = selected
                profiles = list(db.execute(select(cookie_profiles).join(links,
                    (links.c.instance_id == cookie_profiles.c.scope) & (links.c.profile_id == cookie_profiles.c.id)
                    ).where(cookie_profiles.c.deleted == 0, links.c.account_id.in_([r['id'] for r in rows]))).mappings()) if rows else []
            nodes, inventory = self.inventory(db, profiles)
        by_id = {r['id']: r for r in rows}
        for p in inventory:
            p['email'] = (by_id.get(p['account_id']) or {}).get('email')
        if cookies:
            items = inventory
        else:
            grouped = {}
            for p in inventory:
                if p['account_id']:
                    grouped.setdefault(p['account_id'], []).append(p)
            items = []
            for row in rows:
                members = grouped.get(row['id'], [])
                items.append(self.account_item(dict(row), members, nodes))
        if identity:
            item = next((r for r in items if r['id'] == identity), None)
            if not item:
                raise NotFound('账号资料不存在。')
            return item
        if not sql_paged:
            items = [r for r in items if (not auth or (r['auth']['state'] if cookies else r['auth_state']) == auth)
                and (not download or r['download_state'] == download)]
            total, items = len(items), items[(page-1)*size:page*size]
        return {'items':items, 'total':total, 'page':page, 'size':size,
            'instances':[{k:r[k] for k in ('id','name','online')} for r in nodes.values()]}

    def import_preview(self, rows, mode):
        names = [r['normalized_email'] for r in rows if not r.get('error')]
        with self.store.connect() as db:
            existing = {r['normalized_email']:dict(r) for r in db.execute(select(accounts.c.normalized_email,
                accounts.c.id, accounts.c.version).where(accounts.c.normalized_email.in_(names))).mappings()}
        result, versions = [], {}
        for row in rows:
            if row.get('error'):
                result.append({'line': row['line'], 'action':'error', 'error':row['error']})
                continue
            saved = existing.get(row['normalized_email'])
            versions[row['normalized_email']] = {'id':saved['id'],'version':saved['version']} if saved else None
            result.append({'line':row['line'],'email':row['email'], 'password':'••••••',
                'extra':'••••••' if row['secrets'].get('extra') else '',
                'action':('update' if mode == 'update' else 'skip') if saved else 'create'})
        return result, versions

    def import_commit(self, rows, versions, mode, instance, actor):
        if any(r.get('error') for r in rows):
            raise ValueError('请修正全部错误行后重新预览。')
        counts = {'created':0, 'updated':0, 'skipped':0}
        with self.transaction() as db:
            key = self.key(db)
            target = self.validate_instance(db, instance)
            for r in rows:
                saved = db.execute(select(accounts).where(accounts.c.normalized_email == r['normalized_email']).with_for_update()).mappings().first()
                expected = versions.get(r['normalized_email'])
                actual = {'id':saved['id'], 'version':saved['version']} if saved else None
                if actual != expected:
                    raise Conflict('预览后账号资料发生变化，请重新预览；本批次尚未写入。')
                if saved:
                    if mode != 'update':
                        counts['skipped'] += 1
                        continue
                    secrets = {**key.decrypt(saved), **r['secrets']}
                    db.execute(accounts.update().where(accounts.c.id == saved['id']).values(
                        secrets_ciphertext=key.encrypt(saved['id'], secrets), version=saved['version']+1, updated_at=time.time()))
                    self.record(db, saved['id'], actor, 'import_update')
                    counts['updated'] += 1
                else:
                    identity = uuid.uuid4().hex
                    self.owner(db, r['normalized_email'], target)
                    db.execute(accounts.insert().values(id=identity,email=r['email'],normalized_email=r['normalized_email'],
                        note='',instance_id=target,version=1,key_id=key.key_id,secrets_ciphertext=key.encrypt(identity,r['secrets']),
                        created_at=time.time(),updated_at=time.time()))
                    self.record(db, identity, actor, 'import_create')
                    counts['created'] += 1
        return counts
