import { onUnmounted } from 'vue';
import { ElMessageBox } from 'element-plus';
import 'element-plus/es/components/message-box/style/css';

interface PendingEdit {
  dirty: boolean;
  busy?: boolean;
}
const guards = new Map<symbol, () => PendingEdit>();
let confirming: Promise<boolean> | undefined;
export function useLeaveGuard(read: () => PendingEdit) {
  const key = Symbol();
  guards.set(key, read);
  onUnmounted(() => guards.delete(key));
}
export function hasPendingEdits() {
  return [...guards.values()].some((read) => {
    const value = read();
    return value.dirty || value.busy;
  });
}
export function confirmNavigation(): Promise<boolean> {
  if (confirming) return confirming;
  // Native modal dialogs make body-level popups inert, regardless of z-index.
  const appendTo =
    [...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].at(-1) || document.body;
  const states = [...guards.values()].map((read) => read());
  if (states.some((value) => value.busy))
    return ElMessageBox.alert('正在读取或提交操作，请等待完成后再离开。', '操作进行中', {
      appendTo,
    }).then(
      () => false,
      () => false,
    );
  if (!states.some((value) => value.dirty)) return Promise.resolve(true);
  confirming = ElMessageBox.confirm('离开会放弃当前页面未保存的修改。', '有未保存的修改', {
    appendTo,
    confirmButtonText: '放弃并继续',
    cancelButtonText: '继续编辑',
    type: 'warning',
  })
    .then(
      () => true,
      () => false,
    )
    .finally(() => {
      confirming = undefined;
    });
  return confirming;
}
