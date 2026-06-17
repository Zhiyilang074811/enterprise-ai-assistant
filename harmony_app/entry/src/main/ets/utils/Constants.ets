/**
 * 应用常量配置
 */
export class Constants {
  // 后端API地址 - 根据实际部署情况修改
  static readonly BASE_URL: string = 'http://10.0.2.2:6090'; // 模拟器使用10.0.2.2访问主机
  // static readonly BASE_URL: string = 'http://localhost:6090'; // 真机使用localhost或实际IP
  
  // API端点
  static readonly API_AUTH: string = `${Constants.BASE_URL}/api/auth`;
  static readonly API_BALANCE: string = `${Constants.BASE_URL}/api/balance`;
  static readonly API_APP_CONFIG: string = `${Constants.BASE_URL}/api/public/app-config`;
  static readonly API_TENANT_CONFIG: string = `${Constants.BASE_URL}/api/public/tenant-app-config`;
  static readonly API_CHAT: string = `${Constants.BASE_URL}/api/public/chat`;
  
  // 存储键名
  static readonly KEY_PHONE: string = 'user_phone';
  static readonly KEY_TOKEN: string = 'user_token';
  static readonly KEY_TENANT_ID: string = 'tenant_id';
  static readonly KEY_AGENT_ID: string = 'agent_id';
  
  // 默认租户ID和智能体ID（可从配置获取）
  static readonly DEFAULT_TENANT_ID: string = 'default';
}
