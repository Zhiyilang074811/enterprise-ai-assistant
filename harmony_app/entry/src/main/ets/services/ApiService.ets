import { http } from '@kit.NetworkKit';
import { Constants } from '../utils/Constants';
import { AuthResponse, TenantConfigResponse } from '../models/Agent';
import { KnowledgeHit } from '../models/Message';

/**
 * API服务封装
 */
export class ApiService {
  private static instance: ApiService;
  
  private constructor() {}
  
  /**
   * 获取单例实例
   */
  static getInstance(): ApiService {
    if (!ApiService.instance) {
      ApiService.instance = new ApiService();
    }
    return ApiService.instance;
  }
  
  /**
   * 发送HTTP POST请求
   */
  private async post<T>(url: string, data: Record<string, any>): Promise<T> {
    const httpRequest = http.createHttp();
    
    try {
      const response = await httpRequest.request(url, {
        method: http.RequestMethod.POST,
        header: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        extraData: JSON.stringify(data),
        expectDataType: http.HttpDataType.STRING,
        connectTimeout: 30000,
        readTimeout: 60000
      });
      
      if (response.responseCode === 200 || response.responseCode === 201) {
        return JSON.parse(response.result as string) as T;
      } else {
        throw new Error(`HTTP Error: ${response.responseCode}`);
      }
    } finally {
      httpRequest.destroy();
    }
  }
  
  /**
   * 发送HTTP GET请求
   */
  private async get<T>(url: string, params?: Record<string, string>): Promise<T> {
    const httpRequest = http.createHttp();
    
    try {
      let requestUrl = url;
      if (params) {
        const queryString = Object.entries(params)
          .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
          .join('&');
        requestUrl = `${url}?${queryString}`;
      }
      
      const response = await httpRequest.request(requestUrl, {
        method: http.RequestMethod.GET,
        header: {
          'Accept': 'application/json'
        },
        expectDataType: http.HttpDataType.STRING,
        connectTimeout: 30000,
        readTimeout: 30000
      });
      
      if (response.responseCode === 200) {
        return JSON.parse(response.result as string) as T;
      } else {
        throw new Error(`HTTP Error: ${response.responseCode}`);
      }
    } finally {
      httpRequest.destroy();
    }
  }
  
  /**
   * 用户登录
   */
  async login(phone: string, password: string): Promise<AuthResponse> {
    return this.post<AuthResponse>(Constants.API_AUTH, { phone, password });
  }
  
  /**
   * 获取租户配置
   */
  async getTenantConfig(phone?: string, tenantId?: string, agentId?: string): Promise<TenantConfigResponse> {
    const params: Record<string, string> = {};
    if (phone) params.phone = phone;
    if (tenantId) params.tenant_id = tenantId;
    if (agentId) params.agent_id = agentId;
    
    return this.get<TenantConfigResponse>(Constants.API_TENANT_CONFIG, params);
  }
  
  /**
   * 查询余额
   */
  async getBalance(phone: string): Promise<{ ok: boolean; balance: number; msg?: string }> {
    return this.get(Constants.API_BALANCE, { phone });
  }
  
  /**
   * 发送聊天消息（SSE流式响应）
   */
  async streamChat(
    tenantId: string,
    agentId: string,
    question: string,
    onChunk: (content: string, knowledgeHits?: KnowledgeHit[]) => void,
    onComplete: () => void,
    onError: (error: string) => void
  ): Promise<void> {
    const httpRequest = http.createHttp();
    
    try {
      const response = await httpRequest.request(Constants.API_CHAT, {
        method: http.RequestMethod.POST,
        header: {
          'Content-Type': 'application/json',
          'Accept': 'text/event-stream'
        },
        extraData: JSON.stringify({
          tenant_id: tenantId,
          agent_id: agentId,
          question: question
        }),
        expectDataType: http.HttpDataType.STRING,
        connectTimeout: 30000,
        readTimeout: 120000
      });
      
      if (response.responseCode === 200) {
        const result = response.result as string;
        this.parseSSE(result, onChunk, onComplete);
      } else {
        onError(`HTTP Error: ${response.responseCode}`);
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : '请求失败');
    } finally {
      httpRequest.destroy();
    }
  }
  
  /**
   * 解析SSE响应
   */
  private parseSSE(
    data: string,
    onChunk: (content: string, knowledgeHits?: KnowledgeHit[]) => void,
    onComplete: () => void
  ): void {
    const lines = data.split('\n');
    let knowledgeHits: KnowledgeHit[] | undefined;
    
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const jsonStr = line.slice(6).trim();
        
        if (jsonStr === '[DONE]') {
          onComplete();
          return;
        }
        
        try {
          const json = JSON.parse(jsonStr);
          
          if (json.knowledge_hits) {
            knowledgeHits = json.knowledge_hits;
          }
          
          if (json.content) {
            onChunk(json.content, knowledgeHits);
          }
        } catch (e) {
          // 忽略解析错误
        }
      }
    }
    
    onComplete();
  }
}
