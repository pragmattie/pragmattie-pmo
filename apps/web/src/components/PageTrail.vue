<script setup lang="ts">
import { computed } from 'vue'
import { type RouteLocationRaw, useRoute } from 'vue-router'

// The way back from a detail page: a breadcrumb trail from its menu page (for example
// Projects › Harbor Analytics › Risks) and, on a page opened from the Inbox (`?from=inbox`), Back
// to Inbox, which returns with the same item expanded (`?item=<key>` becomes `?open=<key>`).
export interface Crumb {
  title: string
  to?: RouteLocationRaw
}

const props = defineProps<{ crumbs: Crumb[] }>()
const route = useRoute()

const back = computed(() => {
  if (route.query.from !== 'inbox') return null
  const item = route.query.item
  return { name: 'inbox', query: typeof item === 'string' && item ? { open: item } : {} }
})
// The last crumb is the page itself, so it is not a link.
const items = computed(() =>
  props.crumbs.map((crumb, index) => ({
    title: crumb.title,
    to: index < props.crumbs.length - 1 ? crumb.to : undefined,
    disabled: index === props.crumbs.length - 1,
  })),
)
</script>

<template>
  <div class="mb-2" data-test="page-trail">
    <v-btn
      v-if="back"
      :to="back"
      variant="text"
      color="primary"
      prepend-icon="$prev"
      class="px-1 mb-1"
      data-test="back-to-inbox"
    >
      Back to Inbox
    </v-btn>
    <v-breadcrumbs :items="items" divider="›" class="px-0 py-1" data-test="breadcrumbs" />
  </div>
</template>
