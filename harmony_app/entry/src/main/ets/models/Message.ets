/**
 * 消息类型枚举
 */
export enum MessageType {
  USER = 'user',
  ASSISTANT = 'assistant',
  SYSTEM = 'system'
}

/**
 * 消息模型
 */
export class Message {
  id: string;
  type: MessageType;
  content: string;
  timestamp: number;
  isLoading?: boolean;
  knowledgeHits?: Array<KnowledgeHit>;
  
  constructor(id: string, type: MessageType, content: string) {
    this.id = id;
    this.type = type;
    this.content = content;
    this.timestamp = Date.now();
  }
  
  /**
   * 创建用户消息
   */
  static createUserMessage(content: string): Message {
    return new Message(`user_${Date.now()}`, MessageType.USER, content);
  }
  
  /**
   * 创建助手消息
   */
  static createAssistantMessage(content: string, isLoading: boolean = false): Message {
    const msg = new Message(`assistant_${Date.now()}`, MessageType.ASSISTANT, content);
    msg.isLoading = isLoading;
    return msg;
  }
}

/**
 * 知识命中项
 */
export interface KnowledgeHit {
  source: string;
  content: string;
  score: number;
  tier: string;
  tier_label: string;
  tags: string[];
  library_id?: string;
  library_name?: string;
  category_id?: string;
  category_name?: string;
}
