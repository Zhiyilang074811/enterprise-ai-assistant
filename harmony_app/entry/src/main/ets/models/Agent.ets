/**
 * 智能体模型
 */
export interface Agent {
  agent_id: string;
  name: string;
  description: string;
  avatar: string;
  status: string;
  enabled: boolean;
  is_default: boolean;
  workflow_id: string;
  knowledge_scope: Record<string, any>;
  tool_scope: string[];
  mcp_servers: string[];
  recommended_questions: string[];
  streaming: boolean;
  fallback_enabled: boolean;
  fallback_message: string;
  show_recommended: boolean;
  model: string;
  supports_image: boolean;
  capability_label: string;
}

/**
 * 应用配置
 */
export interface AppConfig {
  chat_title: string;
  chat_tagline: string;
  logo: string;
  welcome_message: string;
  input_placeholder: string;
  recommended_questions: string[];
  [key: string]: any;
}

/**
 * 登录响应
 */
export interface AuthResponse {
  ok: boolean;
  msg?: string;
  phone?: string;
  tenant_id?: string;
  tenant_name?: string;
  display_name?: string;
}

/**
 * 租户配置响应
 */
export interface TenantConfigResponse {
  ok: boolean;
  config: AppConfig;
  agent?: Agent;
}
