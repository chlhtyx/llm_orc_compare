/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端基址,默认空(同源,走 Vite 代理)。生产可设为绝对地址。 */
  readonly VITE_API_BASE?: string
  /** 开发用 API Key(对应后端 X-API-Key;生产应改由用户在界面填写) */
  readonly VITE_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
