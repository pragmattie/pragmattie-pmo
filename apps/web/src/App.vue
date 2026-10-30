<template>
  <v-app>
    <v-app-bar color="primary" flat>
      <v-app-bar-title data-test="brand">PragMattie PMO</v-app-bar-title>
    </v-app-bar>
    <v-main>
      <router-view />
    </v-main>
    <v-footer app color="primary" class="justify-center text-body-2" data-test="footer">
      &copy; PragMattie Growth Partners, LLC. All rights reserved.
    </v-footer>
  </v-app>
</template>
