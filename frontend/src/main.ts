import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import './style.css';
import './workspace.css';
import './design.css';
import { router } from './router';

const app = createApp(App).use(createPinia()).use(router);
// Resolve standalone routes before the workspace can start protected API calls.
void router.isReady().then(() => app.mount('#frontend-app'));
