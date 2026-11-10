'use strict';

// Settings layout lives in templates; only navigation and validation belong here.
(() => {
  const form = document.querySelector('#settings-form');
  if (!form) return;
  const tabs = [...document.querySelectorAll('.settings-tabs [role="tab"]')];
  const panes = new Map([...form.querySelectorAll('[data-settings-category]')]
    .map(pane => [pane.dataset.settingsCategory, pane]));
  const select = (key, focus = false) => {
    if (!panes.has(key)) return;
    for (const tab of tabs) {
      const active = tab.dataset.settingsTarget === key;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      panes.get(tab.dataset.settingsTarget).hidden = !active;
      if (active && focus) tab.focus();
    }
    if (typeof renderConcurrency === 'function') renderConcurrency();
  };
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-settings-target]');
    if (target?.dataset.settingsTarget === 'accounts' && typeof selectAccountTab === 'function') {
      setView('accounts'); selectAccountTab('cookies');
    } else if (target) select(target.dataset.settingsTarget);
  });
  document.querySelector('.settings-tabs').addEventListener('keydown', event => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 :
      (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    select(tabs[next].dataset.settingsTarget, true);
  });
  // Native validation must reveal a required field even in another category.
  let revealingInvalid = false;
  form.addEventListener('invalid', event => {
    if (revealingInvalid) return;
    revealingInvalid = true;
    setTimeout(() => { revealingInvalid = false; }, 0);
    const pane = event.target.closest('[data-settings-category]');
    if (pane) select(pane.dataset.settingsCategory);
    for (let node = event.target.parentElement; node && node !== form; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
  }, true);
})();
