import type { RuntimeTask } from './contracts';

/** Only current transfers contribute; waiting and completed tasks can retain an old rate. */
export function downloadSpeed(tasks: RuntimeTask[], now: number) {
  let transferring = 0;
  let measured = 0;
  let bytesPerSecond = 0;
  for (const task of tasks) {
    const progress = task.progress;
    if (task.status !== 'running' || progress?.stage !== 'downloading') continue;
    transferring++;
    const reported = task.progress_updated_at || progress._reported_at;
    const changed = progress.last_bytes_at;
    const speed = progress.speed;
    if (
      !reported ||
      now - reported > 10 ||
      reported - now > 5 ||
      (changed && now - changed > 10) ||
      speed == null ||
      !Number.isFinite(speed) ||
      speed < 0
    )
      continue;
    measured++;
    bytesPerSecond += speed;
  }
  return {
    transferring,
    measured,
    bytesPerSecond: transferring && !measured ? null : bytesPerSecond,
  };
}

export function speedNumber(value: number | null | undefined) {
  return value != null && Number.isFinite(value) && value >= 0
    ? value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : '—';
}
