export function isWaitingStage(stage: string) {
  return /^(waiting_|disk_wait$|scheduling_wait$|retrying$)/.test(stage);
}

export function sampleNow(sample: number, receivedAt: number, monotonicNow: number) {
  return sample + Math.max(0, monotonicNow - receivedAt) / 1000;
}

export function waitCountdown(until: number | undefined, now: number, fresh: boolean) {
  if (!fresh || !Number.isFinite(now) || now <= 0) return '等待新鲜采样';
  if (until == null || !Number.isFinite(until) || until <= 0) return '结束时间未定';
  const remaining = Math.ceil(until - now);
  if (remaining <= 0) return '已到期，等待状态更新';
  const pad = (value: number) => String(value).padStart(2, '0');
  const days = Math.floor(remaining / 86400);
  const hours = Math.floor((remaining % 86400) / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const seconds = remaining % 60;
  return `${days ? days + '天 ' : ''}${days || hours ? pad(hours) + ':' : ''}${pad(minutes)}:${pad(seconds)} 后检查`;
}
