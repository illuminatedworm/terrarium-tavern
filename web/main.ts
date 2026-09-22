import { createApp } from 'vue';
import App from './App.vue';
import AppNotification from './components/common/AppNotification.vue';
import './styles/style.css';
createApp(App).component('AppNotification', AppNotification).mount('#app');
