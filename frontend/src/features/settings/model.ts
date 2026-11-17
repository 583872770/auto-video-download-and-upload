import {
  constraints,
  taskStorageKeys,
  type ConfigKey,
  type ConsoleConfig,
} from '../../generated/config';

export interface ValidationIssue {
  key: ConfigKey;
  message: string;
}

export function canonical(key: ConfigKey, value: unknown): string {
  return JSON.stringify(value) ?? '';
}

export function changes(
  baseline: ConsoleConfig | null,
  draft: ConsoleConfig,
): Partial<ConsoleConfig> {
  if (!baseline) return {};
  const changed: Partial<ConsoleConfig> = {};
  for (const key of Object.keys(baseline) as ConfigKey[]) {
    if (key.endsWith('_configured') || (taskStorageKeys as readonly string[]).includes(key))
      continue;
    if (canonical(key, draft[key]) !== canonical(key, baseline[key])) {
      Object.assign(changed, { [key]: draft[key] });
    }
  }
  return changed;
}

export function unconfirmed(submitted: Partial<ConsoleConfig>, saved: ConsoleConfig): ConfigKey[] {
  return (Object.keys(submitted) as ConfigKey[]).filter((key) => {
    return canonical(key, submitted[key]) !== canonical(key, saved[key]);
  });
}

export function validate(config: ConsoleConfig): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  for (const [name, range] of Object.entries(constraints)) {
    if (!range || (taskStorageKeys as readonly string[]).includes(name)) continue;
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
  if (config.disk_resume_free_gb < config.disk_min_free_gb) {
    issues.push({ key: 'disk_resume_free_gb', message: '恢复水位不能低于暂停水位。' });
  }
  if (config.disk_upload_limit_mb > config.disk_import_budget_mb) {
    issues.push({ key: 'disk_upload_limit_mb', message: '不能超过导入及上传临时文件预算。' });
  }
  return issues;
}
