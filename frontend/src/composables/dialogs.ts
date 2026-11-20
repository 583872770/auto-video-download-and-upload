import { ElMessageBox, type ElMessageBoxOptions } from 'element-plus';
import 'element-plus/es/components/message-box/style/css';

function options(value: ElMessageBoxOptions = {}): ElMessageBoxOptions {
  return {
    ...value,
    closeOnClickModal: true,
    // Confirmations opened inside a native modal must stay in its top layer.
    appendTo:
      [...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].at(-1) || document.body,
  };
}

export const messageBox = {
  alert(message: string, title: string, value?: ElMessageBoxOptions) {
    return ElMessageBox.alert(message, title, options(value));
  },
  confirm(message: string, title: string, value?: ElMessageBoxOptions) {
    return ElMessageBox.confirm(message, title, options(value));
  },
  prompt(message: string, title: string, value?: ElMessageBoxOptions) {
    return ElMessageBox.prompt(message, title, options(value));
  },
};

export async function confirmAction(message: string, title = '操作确认') {
  try {
    await messageBox.confirm(message, title, {
      confirmButtonText: '确认',
      cancelButtonText: '取消',
      type: 'warning',
    });
    return true;
  } catch {
    return false;
  }
}
