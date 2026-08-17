// 控制台登录状态与口令登录,对齐后端 /api/v1/auth/*。
import { request } from './client'

/**
 * 免鉴权端点返回的登录状态。
 * auth_required=false 表示未配置 DC_CONSOLE_PASSWORD,前端无需登录。
 */
export interface ConsoleAuthStatus {
  auth_required: boolean
  authenticated: boolean
}

/** 查询登录状态(免鉴权端点,路由守卫每次跳转调用)。 */
export function getAuthStatus(): Promise<ConsoleAuthStatus> {
  return request('/api/v1/auth/status')
}

/** 登录成功后后端下发会话 Cookie(同源自动携带,无需前端保存)。失败抛 401/429 ApiError。 */
export function loginConsole(password: string): Promise<ConsoleAuthStatus> {
  return request('/api/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
}

/** 退出登录:清除会话 Cookie。 */
export function logoutConsole(): Promise<{ status: string }> {
  return request('/api/v1/auth/logout', { method: 'POST' })
}
