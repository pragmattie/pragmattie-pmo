import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { makeVuetify } from './plugins/vuetify'
import { makeRouter } from './router'

createApp(App).use(createPinia()).use(makeRouter()).use(makeVuetify()).mount('#app')
