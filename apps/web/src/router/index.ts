import { createMemoryHistory, createRouter, createWebHistory } from 'vue-router'

import HomeView from '../views/HomeView.vue'

export function makeRouter(memory = false) {
  return createRouter({
    history: memory ? createMemoryHistory() : createWebHistory(),
    routes: [{ path: '/', name: 'home', component: HomeView }],
  })
}
