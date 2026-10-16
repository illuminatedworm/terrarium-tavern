import { createApp } from 'vue';
import App from './App.vue';
import AppNotification from './components/common/AppNotification.vue';
// Cascade order: tokens → foundation → frame → areas → floating layers → pages.
import './styles/tokens.css';
import './styles/base.css';
import './styles/shell.css';
import './styles/story.css';
import './styles/composer.css';
import './styles/overlays.css';
import './styles/pages.css';
import './styles/settings.css';
createApp(App).component('AppNotification', AppNotification).mount('#app');
