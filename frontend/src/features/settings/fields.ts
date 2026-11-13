import type { ConfigKey } from '../../generated/config';

export type Section =
  | 'storage'
  | 'download'
  | 'performance'
  | 'maintenance'
  | 'diagnostics'
  | 'delivery';
export interface Field {
  key: ConfigKey;
  label: string;
  section: Section;
  help?: string;
  placeholder?: string;
  options?: Array<[string | boolean, string]>;
  password?: boolean;
}
const select = (
  key: ConfigKey,
  label: string,
  section: Section,
  options: Field['options'],
  help?: string,
): Field => ({ key, label, section, options, help });
const field = (key: ConfigKey, label: string, section: Section, help?: string): Field => ({
  key,
  label,
  section,
  help,
});
export const groups = {
  local: [
    field('path', '素材根目录', 'storage', '相对路径以实例的 config.yaml 所在目录为基准。'),
    field('dir', '子目录（可选）', 'storage'),
    select('shard_downloads', '文件组织方式', 'storage', [
      [true, '按视频 ID 分目录（推荐）'],
      [false, '直接保存到素材目录'],
    ]),
  ],
  oss: [
    field(
      'oss_endpoint',
      'Endpoint',
      'storage',
      '填写 Bucket 所在地域的 HTTPS Endpoint，不填写 Bucket 域名。',
    ),
    field('oss_bucket', 'Bucket 名称', 'storage'),
    field(
      'oss_prefix',
      '对象前缀（可选）',
      'storage',
      '多实例建议使用不同前缀，避免同名文件覆盖。',
    ),
    field('oss_access_key_id', 'AccessKey ID', 'storage', '也可由实例环境变量提供。'),
    {
      ...field(
        'oss_access_key_secret',
        'AccessKey Secret',
        'storage',
        '留空保留现有密钥，保存后不回显。',
      ),
      password: true,
    },
  ],
  ossAdvanced: [
    {
      ...field(
        'oss_security_token',
        'STS Security Token（可选）',
        'storage',
        '临时凭据到期后需要更新。',
      ),
      password: true,
    },
    field('oss_upload_limit', '同时上传文件数', 'storage'),
    field('oss_upload_threads', '每文件上传线程', 'storage'),
    field('oss_part_size_mb', '分片大小（MiB）', 'storage'),
  ],
  retry: [
    field(
      'download_attempts',
      '单个视频最多尝试',
      'download',
      '临时网络错误退避重试，账号失效与视频不存在直接报告。',
    ),
  ],
  subtitles: [
    select('subtitle_mode', '历史任务字幕', 'download', [
      ['optional', '视频优先，字幕后台补下载'],
      ['required', '字幕必须完整'],
      ['off', '不下载字幕'],
    ]),
    field(
      'subtitle_languages',
      '字幕语言代码',
      'download',
      '最多 8 个，以逗号分隔；新任务请使用交付要求。',
    ),
  ],
  performance: [
    select('throughput_mode', '运行模式', 'performance', [
      ['manual', '自定义'],
      ['auto', '自动调节'],
    ]),
    field(
      'worker_count',
      '实例并发上限',
      'performance',
      '启用账号池时按会话容量调度，不使用此值。',
    ),
    field(
      'bandwidth_mbps',
      '公网下载带宽目标（Mbps）',
      'performance',
      '仅自动调节使用，0 表示不设目标。',
    ),
    field('yt5s_browser_count', '浏览器并发数', 'performance', 'yt5s、yt1d、Bergung 共享。'),
    field('fragment_concurrency', '分片并发数', 'performance'),
    field('postprocess_limit', '本地后处理并发', 'performance'),
  ],
  strategy: [
    select('scheduler_mode', '渠道选择策略', 'performance', [
      ['auto', '自动择优'],
      ['ordered_fallback', '按顺序尝试'],
      ['balanced', '同类渠道分流'],
    ]),
    field('auto_channel_attempts', '自动策略最多尝试入口', 'performance'),
    select('shared_parser', '复用解析浏览器', 'performance', [
      [true, '开启（推荐）'],
      [false, '关闭'],
    ]),
    field('download_connection_limit', '本机下载连接预算', 'performance'),
    field('builtin_transfer_limit', '内置渠道传输上限', 'performance', '0 表示不设置此项上限。'),
    field('builtin_resolver_limit', '内置渠道解析上限', 'performance', '0 表示不设置此项上限。'),
  ],
  pool: [
    field(
      'youtube_cookie_pool_worker_limit',
      'Cookie 池下载进程上限',
      'performance',
      '0 表示不设置此项上限；仍受资源和连接预算约束。',
    ),
    field('youtube_cookie_pool_preparation_workers', 'Cookie 池准备进程余量', 'performance'),
  ],
  database: [
    select('db_budget_mode', '预算方式', 'performance', [
      ['auto', '自动核算（推荐）'],
      ['manual', '手动指定'],
    ]),
    field('db_connection_budget', '手动连接预算', 'performance'),
  ],
  disk: [
    field('disk_min_free_gb', '磁盘暂停水位（GiB）', 'maintenance'),
    field('disk_resume_free_gb', '磁盘恢复水位（GiB）', 'maintenance'),
    field('disk_budget_gb', '临时文件总预算（GiB）', 'maintenance'),
    field('disk_upload_limit_mb', '单次清单上传上限（MiB）', 'maintenance'),
  ],
  budgets: [
    field('disk_test_budget_gb', '测试下载预算（GiB）', 'maintenance'),
    field('disk_browser_budget_mb', '浏览器缓存预算（MiB）', 'maintenance'),
    field('disk_import_budget_mb', '导入及上传临时文件预算（MiB）', 'maintenance'),
    field('disk_cache_budget_mb', '诊断及其他缓存预算（MiB）', 'maintenance'),
    field(
      'min_free_gb',
      '下载器预留空间（GB）',
      'maintenance',
      '额外空间门槛，与实例暂停水位同时生效。',
    ),
  ],
  logs: [
    field('disk_success_log_hours', '成功任务日志保留（小时）', 'maintenance'),
    field('disk_failure_log_days', '失败任务日志保留（天）', 'maintenance'),
    field('disk_log_budget_mb', '全部日志预算（MiB）', 'maintenance'),
    field('max_log_mb', '单条日志上限（MB）', 'maintenance'),
    field(
      'log_retention_days',
      '历史日志保留天数',
      'maintenance',
      '0 表示使用分类保留期限；容量预算始终生效。',
    ),
  ],
} satisfies Record<string, Field[]>;
export const qualityFields: Record<string, Field[]> = {};
for (const [provider, heights] of Object.entries({
  yt5s: [360, 720],
  yt1d: [360, 480, 720, 1080, 1440, 2160],
  bergung: [360, 480, 720, 1080, 1440, 2160, 4320],
  downmp3: [360, 480, 720, 1080],
})) {
  qualityFields[provider] = [
    select(`${provider}_quality` as ConfigKey, `${provider} 画质`, 'download', [
      ['best', '尽可能最高画质'],
      ...heights.map((n) => [String(n), `${n}p`] as [string, string]),
    ]),
  ];
  if (provider !== 'yt5s')
    qualityFields[provider].push(
      field(
        `${provider}_timeout` as ConfigKey,
        `${provider} 验证 / 转换等待上限（秒）`,
        'download',
      ),
    );
}
export const allFields = [...Object.values(groups).flat(), ...Object.values(qualityFields).flat()];
