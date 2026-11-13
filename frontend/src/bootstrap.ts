export interface Bootstrap {
  csrf: string;
  fleetEnabled: boolean;
  role: 'admin' | 'operator' | 'viewer';
  cobaltCatalog: Array<{
    host: string;
    status: string;
    reason: string;
    channel: import('./generated/config').DownloadChannel;
  }>;
}

export function bootstrap(): Bootstrap {
  const source = document.getElementById('frontend-bootstrap');
  if (!source?.textContent) throw new Error('页面初始化信息缺失，请刷新页面。');
  return JSON.parse(source.textContent) as Bootstrap;
}
