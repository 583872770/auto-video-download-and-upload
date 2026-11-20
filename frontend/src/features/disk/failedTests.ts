import { diskBytes } from './model';
export type Resolution = 'retry' | 'redownload' | 'discard';
export interface FailedTest {
  task_id: string;
  video_id: string;
  title: string;
  job_id: string | null;
  job_name: string;
  failed_at: string;
  error: string;
  bytes: number;
  attempts: number;
  recommendation: string;
  version: string;
}
export interface FailedTestsPage {
  items: FailedTest[];
  next_cursor: string | null;
}
export const actions: Record<Resolution, string> = {
  retry: '重试本地处理',
  redownload: '重新下载',
  discard: '放弃并清理',
};
export function confirmation(item: FailedTest, action: Resolution, instance: string): string {
  const outcome = {
    retry: '保留现有源文件，重新尝试本地转换和交付，不重新请求源站。',
    redownload: '删除这次测试保留的临时文件，重新排队下载缺项并处理。会重新使用账号和网络流量。',
    discard: '删除这次测试保留的临时文件并放弃剩余处理，不再自动重试；以后需要时必须重新下载。',
  }[action];
  return `实例：${instance}；视频：${item.video_id}，临时文件 ${diskBytes(item.bytes)}。${outcome}失败历史和已登记统计保留。`;
}
