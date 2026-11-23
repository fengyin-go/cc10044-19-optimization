import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SnapshotRestoreConsole from '@/views/SnapshotRestoreConsole.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/snapshot-restore-console', name: 'snapshot-restore-console', component: SnapshotRestoreConsole },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
