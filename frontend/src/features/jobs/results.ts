import type { DeliveryArtifact, OutputKind, SourceDelivery } from '../delivery/model';
export interface SamplePolicy {
  enabled: boolean;
  maximum: number;
  budget_mb: number;
  hours: number;
  failure_hours: number;
  revision: number;
}
export interface TestSample {
  id: string;
  job_id: string;
  task_id: string;
  video_id: string;
  title: string;
  instance_id: string;
  size: number;
  expires: number;
  review: 'pending' | 'accepted' | 'rejected';
  revision: number;
  files: DeliveryArtifact[];
}
export interface VideoResult {
  video_id: string;
  title: string;
  status: string;
  error?: string;
  delivery: SourceDelivery;
  selected_outputs: OutputKind[];
  files: DeliveryArtifact[];
  samples: TestSample[];
  retention?: { message: string; time: number };
  review?: { status: string; actor: string; time: number };
}
export const fileStates = {
  sample: '测试样本可查看',
  stored: '已保存，可查看',
  cleaned: '测试文件已清理',
  none: '暂无可查看文件',
};
export const reviewLabels = {
  pending: '待人工验收',
  accepted: '人工验收通过',
  rejected: '人工验收发现问题',
};
export function expiry(value: number) {
  const hours = (value * 1000 - Date.now()) / 3600000;
  return hours <= 0
    ? '已到期，待自动清理'
    : `${hours < 1 ? '不足 1' : Math.ceil(hours)} 小时后到期`;
}
export function requirementText(status: string, hasFile: boolean, test: boolean) {
  if (status === 'done')
    return hasFile ? '文件可查看' : test ? '校验已通过，文件已清理' : '已登记，文件暂不可查看';
  return (
    (
      {
        pending: '等待处理',
        failed: '处理失败，可补交',
        missing: '上游没有符合要求的内容',
        waived: '已人工接受上游缺失',
      } as Record<string, string>
    )[status] || status
  );
}
