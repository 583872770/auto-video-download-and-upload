import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import './style.css';
import './workspace.css';
import './design.css';
import { router } from './router';

createApp(App).use(createPinia()).use(router).mount('#frontend-app');
