'use strict';

// Share light dismissal across both the workbench and the task center,
// including dialogs added later. Always use close() so normal cleanup runs.
(() => {
  let backdropPress = null;
  let confirmationNumber = 0;

  // Keep confirmation windows dismissible as well. Closing without pressing
  // the affirmative button always resolves to cancellation.
  window.confirmDialog = (message, {title = '请确认', confirmText = '确认'} = {}) => new Promise(resolve => {
    const dialog = document.createElement('dialog');
    const heading = document.createElement('h2');
    heading.id = `confirmation-title-${++confirmationNumber}`;
    heading.textContent = title;
    dialog.setAttribute('aria-labelledby', heading.id);
    const description = document.createElement('p');
    description.className = 'dialog-description';
    description.textContent = message;
    const form = document.createElement('form');
    form.method = 'dialog';
    form.className = 'dialog-footer';
    const cancel = document.createElement('button');
    cancel.type = 'submit';
    cancel.value = 'cancel';
    cancel.className = 'button button-white';
    cancel.textContent = '取消';
    cancel.autofocus = true;
    const confirm = document.createElement('button');
    confirm.type = 'submit';
    confirm.value = 'confirm';
    confirm.className = 'button button-danger';
    confirm.textContent = confirmText;
    form.append(cancel, confirm);
    dialog.append(heading, description, form);
    dialog.addEventListener('close', () => {
      const accepted = dialog.returnValue === 'confirm';
      dialog.remove();
      resolve(accepted);
    }, {once: true});
    document.body.append(dialog);
    dialog.showModal();
  });

  function outsideDialog(event, dialog) {
    if (!(dialog instanceof HTMLDialogElement) || !dialog.open || event.target !== dialog) return false;
    const rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom;
  }

  document.addEventListener('pointerdown', event => {
    if (!event.isPrimary) return;
    backdropPress = event.button === 0 && outsideDialog(event, event.target)
      ? {dialog: event.target, pointerId: event.pointerId} : null;
  }, true);

  document.addEventListener('pointerup', event => {
    if (!backdropPress || !event.isPrimary) return;
    if (event.pointerId !== backdropPress.pointerId || !outsideDialog(event, backdropPress.dialog)) {
      backdropPress = null;
    }
  }, true);
  document.addEventListener('pointercancel', () => { backdropPress = null; }, true);
  document.addEventListener('close', event => {
    if (backdropPress?.dialog === event.target) backdropPress = null;
  }, true);

  document.addEventListener('click', event => {
    const pressed = backdropPress;
    backdropPress = null;
    // Requiring the press to begin outside prevents text selection or a drag
    // from inside the dialog from dismissing it on release over the backdrop.
    if (event.button !== 0 || !pressed || !outsideDialog(event, pressed.dialog)) return;
    event.preventDefault();
    pressed.dialog.close();
  }, true);
})();
