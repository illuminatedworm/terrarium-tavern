import { createApp } from 'vue';
import App from './App.vue';
import AppNotification from './components/common/AppNotification.vue';
import './styles/style.css';
import './styles/composer.css';
import './styles/settings.css';
import './styles/glass.css';
createApp(App).component('AppNotification', AppNotification).mount('#app');
