import { request } from './client'

export interface DeploymentStatus {
  deployment_id: string
  version: string
  mode: 'SERVING' | 'DRAINING' | 'STOPPED'
  drained: boolean
  counts: {
    pending_tasks: number
    running_tasks: number
    pending_callbacks: number
    activities: number
  }
  other_active_deployment: string | null
}

export const getDeploymentStatus = () => request<DeploymentStatus>('/api/v1/deployment')
