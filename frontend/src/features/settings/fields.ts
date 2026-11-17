import type { ConfigKey } from '../../generated/config';

export type Section = 'download' | 'performance' | 'maintenance' | 'diagnostics';
export interface Field {
  key: ConfigKey;
  label: string;
  section: Section;
  help?: string;
  placeholder?: string;
  options?: Array<[string | boolean, string]>;
  password?: boolean;
}
const field = (key: ConfigKey, label: string, section: Section, help?: string): Field => ({
  key,
  label,
  section,
  help,
});
export const groups = {
  retry: [
    field(
      'download_attempts',
      '单个视频最多尝试',
      'download',
      '临时网络错误退避重试，账号失效与视频不存在直接报告。',
    ),
  ],
  performance: [
    field(
      'download_task_limit',
      '下载任务并发上限',
      'performance',
      '0 表示按可用账号自动确定；资源保护会根据 CPU、内存、I/O 和数据库容量收敛。',
    ),
    field(
      'fragment_concurrency',
      '单文件分片并发上限',
      'performance',
      '只对支持分片的媒体生效，不随任务数量平均分配。资源保护会计入分片缓冲内存。',
    ),
    field(
      'postprocess_limit',
      '本地处理并发',
      'performance',
      '转换、校验和文件交付使用独立队列，网络下载完成后即释放账号。',
    ),
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
export const allFields = Object.values(groups).flat();
