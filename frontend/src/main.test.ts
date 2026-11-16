import { expect, it, vi } from 'vitest';

const startup = vi.hoisted(() => {
  let resolve!: () => void;
  const ready = new Promise<void>((done) => {
    resolve = done;
  });
  const app = { use: vi.fn(), mount: vi.fn() };
  app.use.mockReturnValue(app);
  return { app, ready, resolve };
});

vi.mock('vue', () => ({ createApp: () => startup.app }));
vi.mock('pinia', () => ({ createPinia: () => ({}) }));
vi.mock('./App.vue', () => ({ default: {} }));
vi.mock('./router', () => ({ router: { isReady: () => startup.ready } }));

it('waits for the initial route before mounting the workspace and its protected requests', async () => {
  await import('./main');
  expect(startup.app.mount).not.toHaveBeenCalled();
  startup.resolve();
  await startup.ready;
  expect(startup.app.mount).toHaveBeenCalledExactlyOnceWith('#frontend-app');
});
