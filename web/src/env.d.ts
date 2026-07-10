/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端基址,默认空(同源,走 Vite 代理)。生产可设为绝对地址。 */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
