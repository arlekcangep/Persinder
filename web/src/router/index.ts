import { createRouter, createWebHistory } from "vue-router";

import SearchView from "@/views/SearchView.vue";
import SettingsView from "@/views/SettingsView.vue";

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: "/", name: "search", component: SearchView },
    { path: "/settings", name: "settings", component: SettingsView },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
  scrollBehavior: () => ({ top: 0 }),
});
