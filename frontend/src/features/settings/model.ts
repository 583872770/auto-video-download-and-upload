import {
  constraints,
  secretKeys,
  type ConfigKey,
  type ConsoleConfig,
} from '../../generated/config';

export interface ValidationIssue {
  key: ConfigKey;
  message: string;
}

export function canonical(key: ConfigKey, value: unknown): string {
  if (key === 'oss_endpoint') {
    const text = String(value ?? '')
      .trim()
      .replace(/\/+$/, '');
    return text && !text.includes('://') ? 'https://' + text : text;
  }
  if (key === 'oss_prefix') return String(value ?? '').replace(/^\/+|\/+$/g, '');
  if (key === 'download_channels' && Array.isArray(value)) {
    return JSON.stringify(
      value.map((item) => ({
        id: item.id,
        label: item.label,
        adapter: item.adapter,
        site_url: String(item.site_url).replace(/\/+$/, '') + '/',
        api_url: String(item.api_url).replace(/\/+$/, '') + '/',
        upstream_group: item.upstream_group,
        resolver_group: item.resolver_group,
        quota_group: item.quota_group,
        transfer_limit: item.transfer_limit,
        resolver_limit: item.resolver_limit,
        connection_limit: item.connection_limit,
        task_limit: item.task_limit ?? 8,
        weight: item.weight,
        verification: item.verification ?? 'required',
      })),
    );
  }
  return JSON.stringify(value) ?? '';
}

export function changes(
  baseline: ConsoleConfig | null,
  draft: ConsoleConfig,
): Partial<ConsoleConfig> {
  if (!baseline) return {};
  const changed: Partial<ConsoleConfig> = {};
  for (const key of Object.keys(baseline) as ConfigKey[]) {
    if (key.endsWith('_configured')) continue;
    if (secretKeys.some((secret) => secret === key) && !draft[key]) continue;
    if (canonical(key, draft[key]) !== canonical(key, baseline[key])) {
      Object.assign(changed, { [key]: draft[key] });
    }
  }
  return changed;
}

export function unconfirmed(submitted: Partial<ConsoleConfig>, saved: ConsoleConfig): ConfigKey[] {
  return (Object.keys(submitted) as ConfigKey[]).filter((key) => {
    if (key === 'oss_access_key_secret' || key === 'oss_security_token') {
      return !!submitted[key] && !saved[`${key}_configured`];
    }
    return canonical(key, submitted[key]) !== canonical(key, saved[key]);
  });
}

export function validate(config: ConsoleConfig): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  for (const [name, range] of Object.entries(constraints)) {
    if (!range) continue;
    const key = name as ConfigKey,
      value = config[key];
    if (
      typeof value !== 'number' ||
      !Number.isInteger(value) ||
      value < range[0] ||
      value > range[1]
    ) {
      issues.push({ key, message: `请输入 ${range[0]}～${range[1]} 之间的整数。` });
    }
  }
  if (!config.path.trim()) issues.push({ key: 'path', message: '请填写素材根目录。' });
  if (config.storage_mode === 'oss') {
    for (const key of ['oss_endpoint', 'oss_bucket'] as const) {
      if (!config[key].trim()) issues.push({ key, message: '选择 OSS 保存方式后，此项不能为空。' });
    }
    if (config.oss_endpoint.trim()) {
      try {
        const url = new URL(canonical('oss_endpoint', config.oss_endpoint));
        if (
          url.protocol !== 'https:' ||
          !/^oss-[a-z0-9-]+\.aliyuncs\.com$/.test(url.hostname) ||
          url.username ||
          url.password ||
          url.port ||
          url.search ||
          url.hash ||
          url.pathname !== '/'
        )
          throw new Error('invalid');
      } catch {
        issues.push({
          key: 'oss_endpoint',
          message: '请填写阿里云 OSS 的 HTTPS 地域地址，不带 Bucket、端口或路径。',
        });
      }
    }
    if (config.oss_bucket && !/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(config.oss_bucket))
      issues.push({
        key: 'oss_bucket',
        message: 'Bucket 名称须为 3～63 位小写字母、数字或连字符。',
      });
    if (
      new TextEncoder().encode(config.oss_prefix).length > 512 ||
      /[\\\x00-\x1f]/.test(config.oss_prefix) ||
      config.oss_prefix.split('/').some((part) => part === '.' || part === '..')
    )
      issues.push({
        key: 'oss_prefix',
        message: '前缀不能包含反斜杠、控制字符、. 或 .. 目录，最多 512 字节。',
      });
  }
  const languages = config.subtitle_languages.split(',');
  if (
    !languages.length ||
    languages.length > 8 ||
    languages.some((value) => !/^[A-Za-z0-9-]{1,32}$/.test(value.trim()))
  )
    issues.push({ key: 'subtitle_languages', message: '请填写 1～8 个有效语言代码，用逗号分隔。' });
  if (config.disk_resume_free_gb < config.disk_min_free_gb) {
    issues.push({ key: 'disk_resume_free_gb', message: '恢复水位不能低于暂停水位。' });
  }
  if (config.disk_upload_limit_mb > config.disk_import_budget_mb) {
    issues.push({ key: 'disk_upload_limit_mb', message: '不能超过导入及上传临时文件预算。' });
  }
  if (config.download_methods?.length === 0)
    issues.push({ key: 'download_methods', message: '请至少启用一种下载方式。' });
  if (
    config.youtube_cookie_pool.length &&
    config.download_methods?.some((item) => item !== 'yt_dlp')
  ) {
    issues.push({ key: 'download_methods', message: '账号池模式只能使用 yt-dlp 下载方式。' });
  }
  const channels = config.download_channels;
  if (
    channels.length > 16 ||
    new Set(channels.map((item) => item.id)).size !== channels.length ||
    channels.some(
      (item) =>
        !/^[a-zA-Z0-9_-]{1,64}$/.test(item.id) ||
        ['yt5s', 'yt1d', 'yt_dlp', 'downmp3'].includes(item.id) ||
        item.adapter !== 'bergung' ||
        !item.label.trim() ||
        item.label.length > 80 ||
        /[\x00-\x1f]/.test(item.label) ||
        ![item.upstream_group, item.resolver_group, item.quota_group].every((value) =>
          /^[a-zA-Z0-9_-]{1,64}$/.test(value),
        ) ||
        !['site_url', 'api_url'].every((key) => {
          try {
            const url = new URL(item[key as 'site_url']);
            return (
              url.protocol === 'https:' &&
              !url.username &&
              !url.password &&
              !url.search &&
              !url.hash
            );
          } catch {
            return false;
          }
        }) ||
        !Object.entries({
          transfer_limit: 64,
          resolver_limit: 16,
          connection_limit: 128,
          task_limit: 64,
          weight: 10,
        }).every(
          ([key, max]) =>
            Number.isInteger(item[key as 'weight']) &&
            item[key as 'weight'] >= 1 &&
            item[key as 'weight'] <= max,
        ),
    )
  )
    issues.push({
      key: 'download_channels',
      message:
        '请检查渠道的唯一 ID、名称、资源分组、HTTPS 网址及并发范围（传输 1～64、解析 1～16、连接 1～128、任务 1～64、权重 1～10），最多 16 个渠道。',
    });
  return issues;
}
