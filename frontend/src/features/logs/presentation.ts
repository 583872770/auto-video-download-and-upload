export interface LogEntry {
  time: string;
  text: string;
  level: 'info' | 'success' | 'warning' | 'error';
  progress?: boolean;
}

type Event = Record<string, unknown>;

export function formatLogBytes(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '未知';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${index ? value.toFixed(1) : value} ${units[index]}`;
}

function seconds(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${Number(value.toFixed(1))} 秒`
    : '稍后';
}

function language(value: unknown): string {
  const code = typeof value === 'string' ? value : '';
  if (!code) return '所需语言';
  try {
    const name = new Intl.DisplayNames(['zh-CN'], { type: 'language' }).of(code);
    return name && name !== code ? `${name}（${code}）` : code;
  } catch {
    return code;
  }
}

function message(value: unknown, fallback = '执行遇到异常，具体诊断信息见原始日志。'): string {
  const text = typeof value === 'string' ? value : '';
  if (/\b429\b|Too Many Requests/i.test(text)) return '请求被限流（HTTP 429），需要等待后重试。';
  if (/timed? out|timeout/i.test(text)) return '下载请求超时，请检查连接并等待重试。';
  if (/\b403\b|Forbidden/i.test(text)) return '请求被拒绝（HTTP 403），请核对账号权限和连接。';
  if (/\b404\b|not found/i.test(text)) return '请求的资源不存在或链接已经失效。';
  if (/Sign in|not a bot|cookie.*(?:invalid|rotated|expired)/i.test(text))
    return '来源要求登录或验证，请检查该账号的登录状态。';
  return /[\u3400-\u9fff]/.test(text) ? text.replace(/Cookie\s*账号池/g, '账号池') : fallback;
}

function failure(record: Event): string {
  const detail = record.failure;
  if (detail && typeof detail === 'object' && 'message' in detail) return message(detail.message);
  if (record.message) return message(record.message);
  const causes = record.causes;
  if (Array.isArray(causes) && causes[0]?.message) return message(causes[0].message);
  return '执行遇到异常，具体诊断信息见原始日志。';
}

function parseLine(line: string) {
  const match = line.match(/^(\d{4}-\d{2}-\d{2}T\S+)\s+(.*)$/);
  const time = match?.[1] || '';
  const text = (match?.[2] ?? line).trim();
  let record: Event | undefined;
  if (text.startsWith('[event] ')) {
    try {
      const value: unknown = JSON.parse(text.slice(8));
      if (value && typeof value === 'object' && !Array.isArray(value)) record = value as Event;
    } catch {
      // A log chunk can end in the middle of a structured line. The raw text
      // stays intact and the next chunk completes the same line.
    }
  }
  return { time, text, record };
}

/** The raw log remains available; this view describes actions and results. */
export function readableLog(raw: string): LogEntry[] {
  const lines = raw
    .replace(/\x1b\[[0-9;]*m/g, '')
    .split(/\r?\n/)
    .map(parseLine);
  const events = new Set(lines.map((line) => line.record?.event));
  const entries: LogEntry[] = [];
  let transferred: unknown;
  let subtitle = '';
  let trace = false;
  function add(time: string, text: string, level: LogEntry['level'] = 'info', progress = false) {
    if (!text) return;
    const previous = entries.at(-1);
    if (progress && previous?.progress) entries.pop();
    else if (previous?.text === text && previous.level === level) return;
    entries.push({ time, text, level, ...(progress ? { progress: true } : {}) });
  }
  for (const { time, text, record } of lines) {
    if (!text) continue;
    if (record) {
      const name = record.event;
      switch (name) {
        case 'attempt_start':
          transferred = undefined;
          subtitle = '';
          trace = false;
          add(
            time,
            `开始处理任务${typeof record.attempt === 'number' ? `（第 ${record.attempt} 次尝试）` : ''}。`,
          );
          break;
        case 'attempt_released': {
          const reason = record.reason;
          const label =
            reason === 'pipeline_backpressure'
              ? '等待执行条件'
              : reason === 'disk_pressure' || reason === 'disk_full'
                ? '等待磁盘空间'
                : '任务暂时等待';
          const detail = record.message ? message(record.message) : failure(record);
          const delay =
            typeof record.retry_after === 'number'
              ? ` ${seconds(record.retry_after)}后重新检查。`
              : '';
          add(time, `${label}：${detail}${delay} 本次等待不计入失败重试。`, 'warning');
          break;
        }
        case 'cookie_pool_selected':
          add(time, '已分配可用账号。');
          break;
        case 'execution_policy':
          add(
            time,
            `文件保存位置：${record.storage_mode === 'oss' ? 'OSS 云存储' : '本地素材库'}。`,
          );
          break;
        case 'connection_selected':
          add(
            time,
            record.proxy_mode === 'direct' ? '连接方式：直连。' : '连接方式：通过代理访问来源。',
          );
          break;
        case 'provider_start':
          add(time, `使用 ${String(record.provider || '下载器')} 读取视频与字幕信息。`);
          break;
        case 'subtitle_selected': {
          subtitle = language(record.language);
          const sources: Record<string, string> = {
            manual: '作者提供的人工字幕',
            original_auto: '视频原语言自动识别字幕',
            translated: '自动翻译字幕',
          };
          add(
            time,
            `选中字幕：${subtitle}；来源：${sources[String(record.source)] || '来源提供的字幕'}。`,
          );
          break;
        }
        case 'subtitle_processed':
          add(
            time,
            `字幕格式处理完成：${String(record.source_format).toUpperCase()} → ${String(record.target_format).toUpperCase()}（${language(record.language)}）。`,
            'success',
          );
          break;
        case 'local_processing_end':
          if (record.outcome === 'completed') {
            if (record.operation === 'delivery_subtitle') {
              if (!events.has('subtitle_processed'))
                add(time, '字幕格式整理与校验完成。', 'success');
            } else add(time, '文件合并、转换或校验完成。', 'success');
          } else if (record.outcome === 'failed')
            add(time, '本地文件处理失败，具体原因见后续错误。', 'error');
          break;
        case 'provider_end':
          if (typeof record.transferred_bytes === 'number') transferred = record.transferred_bytes;
          if (record.outcome === 'failed')
            add(time, `本次下载器执行未完成：${failure(record)}`, 'warning');
          break;
        case 'attempt_end':
          if (record.outcome === 'succeeded') {
            const heading = record.skipped
              ? '命中下载去重记录，已跳过重复下载。'
              : record.test_mode
                ? '测试任务完成。'
                : '任务交付完成。';
            const count =
              typeof record.file_count === 'number' ? `交付 ${record.file_count} 个文件；` : '';
            const download =
              typeof transferred === 'number' ? `下载数据 ${formatLogBytes(transferred)}；` : '';
            const output =
              typeof record.file_bytes === 'number'
                ? `交付文件 ${formatLogBytes(record.file_bytes)}；`
                : '';
            add(
              time,
              `${heading}${count}${download}${output}本次执行耗时 ${seconds(record.elapsed_seconds)}。`,
              'success',
            );
          } else if (record.retry_scheduled) {
            add(
              time,
              `本次执行未完成，已安排重试：${failure(record)}${typeof record.retry_delay_seconds === 'number' ? ` ${seconds(record.retry_delay_seconds)}后重新检查。` : ''}`,
              'warning',
            );
          } else if (record.outcome === 'lease_lost') {
            add(time, '当前执行已失去任务租约并停止，请以任务最新状态为准。', 'warning');
          } else add(time, `任务执行未完成：${failure(record)}`, 'error');
          break;
        case 'account_auth_settled':
          add(time, '当前账号需要重新检测登录或完成验证，其他健康账号继续执行。', 'warning');
          break;
        case 'account_limit_settled':
          add(
            time,
            `当前账号已进入冷却${typeof record.retry_after === 'number' ? `，${seconds(record.retry_after)}后重新检查` : ''}，其他健康账号继续执行。`,
            'warning',
          );
          break;
        case 'task_log_unavailable':
          add(time, '任务日志暂时无法写入，诊断记录已转入实例运行日志。', 'warning');
          break;
        default:
          if (record.outcome === 'failed' || record.error_type)
            add(time, failure(record), 'warning');
      }
      continue;
    }
    if (text.startsWith('[event] ')) continue;
    if (/^\[\w+\] 任务开始/.test(text)) {
      if (!events.has('attempt_start')) add(time, '开始处理任务。');
      continue;
    }
    if (/^任务执行完成/.test(text)) {
      if (!events.has('attempt_end')) add(time, text, 'success');
      continue;
    }
    if (/完成：传输|^本任务下载方式：|^开始下载，共/.test(text)) continue;
    if (text.startsWith('WARNING:')) {
      if (/impersonat/i.test(text) && /no .*target|not available|not supported/i.test(text))
        add(
          time,
          '未启用浏览器请求模拟，下载器继续使用可用请求方式；这条提示本身不表示下载失败。',
          'warning',
        );
      else if (/DRM protected/i.test(text))
        add(time, '部分媒体格式受 DRM 保护，已跳过这些格式并继续选择可用格式。', 'warning');
      else
        add(
          time,
          /[\u3400-\u9fff]/.test(text)
            ? text.replace(/^WARNING:\s*/, '提示：')
            : `下载器提示：${message(text, '下载器返回了一条提示，原文保留在原始日志中。')}`,
          'warning',
        );
      continue;
    }
    if (text.startsWith('ERROR:')) {
      add(time, message(text), 'error');
      continue;
    }
    const wait = text.match(/^\[download\] Sleeping ([\d.]+) seconds/);
    if (wait) {
      add(time, `按下载请求间隔等待 ${seconds(Number(wait[1]))}，随后继续请求。`);
      continue;
    }
    if (text.startsWith('[download] Destination:')) {
      add(time, subtitle ? `开始下载${subtitle}字幕。` : '开始下载媒体文件。');
      continue;
    }
    const progress = text.match(/^\[download\]\s+([\d.]+)%/);
    if (progress) {
      add(time, `下载进度：${Number(progress[1]).toFixed(1)}%。`, 'info', true);
      continue;
    }
    if (
      /^\[(youtube|info|download|jsc|debug|Merger|ExtractAudio|VideoRemuxer|SubtitlesConvertor)[\]: ]/.test(
        text,
      )
    )
      continue;
    if (text.startsWith('Traceback ')) {
      if (!trace) add(time, '执行出现异常，详细调用记录已保留在原始日志中。', 'error');
      trace = true;
      continue;
    }
    if (trace && (/^File |^\^|^[~\s]*\^/.test(text) || !/[\u3400-\u9fff]/.test(text))) continue;
    if (/[\u3400-\u9fff]/.test(text)) add(time, text);
  }
  return entries;
}

export function logTime(value: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}
