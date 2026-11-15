import type { DeliveryArtifact } from './model';

export function subtitleLanguage(code: string): string {
  if (!code) return '语言未记录';
  try {
    const name = new Intl.DisplayNames(['zh-CN'], { type: 'language' }).of(code);
    return name && name !== code ? `${name}（${code}）` : code;
  } catch {
    return code;
  }
}

export function subtitleDescription(artifact: DeliveryArtifact): string {
  const sources = {
    manual: '人工字幕',
    original_auto: '原语言自动识别字幕',
    translated: '自动翻译字幕',
  };
  const source = artifact.subtitle_source;
  return `${subtitleLanguage(artifact.language)} · ${source ? sources[source] || '来源未记录' : '来源未记录'}`;
}
