import { ApiService } from '../services/ApiService';
import { Constants } from '../utils/Constants';
import { Message, MessageType, KnowledgeHit } from '../models/Message';
import { AppConfig, Agent } from '../models/Agent';
import { preferences } from '@kit.ArkData';
import { router } from '@kit.ArkUI';

/**
 * 聊天页面
 */
@Entry
@Component
struct Chat {
  @State messages: Message[] = [];
  @State inputText: string = '';
  @State isLoading: boolean = false;
  @State appConfig: AppConfig | null = null;
  @State currentAgent: Agent | null = null;
  @State phone: string = '';
  @State tenantId: string = Constants.DEFAULT_TENANT_ID;
  @State agentId: string = '';
  @State showKnowledge: boolean = false;
  @State currentKnowledgeHits: KnowledgeHit[] = [];
  
  private apiService: ApiService = ApiService.getInstance();
  private scroller: Scroller = new Scroller();
  
  async aboutToAppear(): Promise<void> {
    await this.loadUserData();
    await this.loadConfig();
    this.addWelcomeMessage();
  }
  
  /**
   * 加载用户数据
   */
  async loadUserData(): Promise<void> {
    try {
      const prefs = preferences.getPreferencesSync(getContext(), { name: 'app_prefs' });
      this.phone = (await prefs.get(Constants.KEY_PHONE, '')) as string;
      this.tenantId = (await prefs.get(Constants.KEY_TENANT_ID, Constants.DEFAULT_TENANT_ID)) as string;
      this.agentId = (await prefs.get(Constants.KEY_AGENT_ID, '')) as string;
    } catch (error) {
      console.error('Load user data error:', error);
    }
  }
  
  /**
   * 加载应用配置
   */
  async loadConfig(): Promise<void> {
    try {
      const response = await this.apiService.getTenantConfig(
        this.phone,
        this.tenantId,
        this.agentId
      );
      
      if (response.ok) {
        this.appConfig = response.config;
        this.currentAgent = response.agent || null;
      }
    } catch (error) {
      console.error('Load config error:', error);
    }
  }
  
  /**
   * 添加欢迎消息
   */
  addWelcomeMessage(): void {
    const welcomeText = this.appConfig?.welcome_message || '您好！我是您的智能助手，有什么可以帮您的吗？';
    const welcomeMsg = Message.createAssistantMessage(welcomeText);
    this.messages = [welcomeMsg];
  }
  
  /**
   * 发送消息
   */
  async sendMessage(): Promise<void> {
    const text = this.inputText.trim();
    if (!text || this.isLoading) {
      return;
    }
    
    // 添加用户消息
    const userMsg = Message.createUserMessage(text);
    this.messages.push(userMsg);
    this.inputText = '';
    this.scrollToBottom();
    
    // 添加加载中的助手消息
    const assistantMsg = Message.createAssistantMessage('', true);
    this.messages.push(assistantMsg);
    this.isLoading = true;
    this.scrollToBottom();
    
    try {
      await this.apiService.streamChat(
        this.tenantId,
        this.agentId || 'agent_default', // 需要根据实际情况获取
        text,
        (content: string, knowledgeHits?: KnowledgeHit[]) => {
          // 更新助手消息内容
          const lastIndex = this.messages.length - 1;
          if (lastIndex >= 0 && this.messages[lastIndex].type === MessageType.ASSISTANT) {
            this.messages[lastIndex].content += content;
            if (knowledgeHits && knowledgeHits.length > 0) {
              this.messages[lastIndex].knowledgeHits = knowledgeHits;
            }
            // 强制更新UI
            this.messages = [...this.messages];
            this.scrollToBottom();
          }
        },
        () => {
          // 完成
          this.isLoading = false;
          const lastIndex = this.messages.length - 1;
          if (lastIndex >= 0) {
            this.messages[lastIndex].isLoading = false;
            this.messages = [...this.messages];
          }
        },
        (error: string) => {
          // 错误
          this.isLoading = false;
          const lastIndex = this.messages.length - 1;
          if (lastIndex >= 0 && this.messages[lastIndex].type === MessageType.ASSISTANT) {
            this.messages[lastIndex].content = '抱歉，发生了错误：' + error;
            this.messages[lastIndex].isLoading = false;
            this.messages = [...this.messages];
          }
        }
      );
    } catch (error) {
      this.isLoading = false;
      console.error('Send message error:', error);
    }
  }
  
  /**
   * 滚动到底部
   */
  scrollToBottom(): void {
    setTimeout(() => {
      this.scroller.scrollEdge(Edge.Bottom);
    }, 100);
  }
  
  /**
   * 退出登录
   */
  async logout(): Promise<void> {
    try {
      const prefs = preferences.getPreferencesSync(getContext(), { name: 'app_prefs' });
      await prefs.clear();
      await prefs.flush();
      
      router.replaceUrl({
        url: 'pages/Index'
      });
    } catch (error) {
      console.error('Logout error:', error);
    }
  }
  
  /**
   * 点击推荐问题
   */
  onRecommendedQuestion(question: string): void {
    this.inputText = question;
    this.sendMessage();
  }
  
  build() {
    Column() {
      // 顶部导航栏
      Row() {
        if (this.currentAgent?.avatar) {
          Image(this.currentAgent.avatar)
            .width(36)
            .height(36)
            .borderRadius(18)
            .objectFit(ImageFit.Cover)
        } else {
          Circle()
            .width(36)
            .height(36)
            .fill('#007DFF')
        }
        
        Column() {
          Text(this.currentAgent?.name || this.appConfig?.chat_title || '智能助手')
            .fontSize(16)
            .fontWeight(FontWeight.Medium)
          
          if (this.currentAgent?.description) {
            Text(this.currentAgent.description)
              .fontSize(12)
              .fontColor('#999999')
          }
        }
        .alignItems(HorizontalAlign.Start)
        .margin({ left: 12 })
        .layoutWeight(1)
        
        Button('退出')
          .fontSize(14)
          .backgroundColor(Color.Transparent)
          .fontColor('#666666')
          .onClick(() => {
            this.logout();
          })
      }
      .width('100%')
      .height(56)
      .padding({ left: 16, right: 16 })
      .border({ width: { bottom: 1 }, color: '#EEEEEE' })
      
      // 消息列表
      List({ scroller: this.scroller }) {
        ForEach(this.messages, (msg: Message) => {
          ListItem() {
            this.MessageItem(msg)
          }
        }, (msg: Message) => msg.id)
      }
      .layoutWeight(1)
      .width('100%')
      .padding({ left: 16, right: 16, top: 12, bottom: 12 })
      .backgroundColor('#F8F8F8')
      
      // 推荐问题
      if (this.appConfig?.recommended_questions && this.appConfig.recommended_questions.length > 0 && this.messages.length <= 1) {
        Scroll() {
          Row({ space: 8 }) {
            ForEach(this.appConfig.recommended_questions, (question: string) => {
              Button(question)
                .fontSize(13)
                .padding({ left: 12, right: 12, top: 8, bottom: 8 })
                .borderRadius(16)
                .backgroundColor('#FFFFFF')
                .fontColor('#333333')
                .onClick(() => {
                  this.onRecommendedQuestion(question);
                })
            })
          }
          .padding({ left: 16, right: 16 })
        }
        .scrollable(ScrollDirection.Horizontal)
        .scrollBar(BarState.Off)
        .width('100%')
        .height(50)
        .backgroundColor('#FFFFFF')
      }
      
      // 底部输入框
      Row() {
        TextInput({ text: this.inputText, placeholder: this.appConfig?.input_placeholder || '请输入您的问题...' })
          .layoutWeight(1)
          .height(44)
          .borderRadius(22)
          .backgroundColor('#F5F5F5')
          .padding({ left: 16, right: 16 })
          .onChange((value: string) => {
            this.inputText = value;
          })
          .onSubmit(() => {
            this.sendMessage();
          })
        
        Button('发送')
          .width(72)
          .height(44)
          .fontSize(15)
          .borderRadius(22)
          .margin({ left: 12 })
          .enabled(!this.isLoading && this.inputText.trim().length > 0)
          .backgroundColor(this.isLoading || !this.inputText.trim() ? '#CCCCCC' : '#007DFF')
          .onClick(() => {
            this.sendMessage();
          })
      }
      .width('100%')
      .padding({ left: 16, right: 16, top: 12, bottom: 12 })
      .backgroundColor('#FFFFFF')
      .border({ width: { top: 1 }, color: '#EEEEEE' })
    }
    .width('100%')
    .height('100%')
  }
  
  /**
   * 消息项组件
   */
  @Builder
  MessageItem(msg: Message) {
    Column() {
      if (msg.type === MessageType.USER) {
        // 用户消息
        Row() {
          Blank()
          
          Text(msg.content)
            .fontSize(15)
            .lineHeight(22)
            .padding({ left: 14, right: 14, top: 10, bottom: 10 })
            .backgroundColor('#007DFF')
            .fontColor('#FFFFFF')
            .borderRadius({ topLeft: 16, topRight: 16, bottomLeft: 16, bottomRight: 4 })
            .constraintSize({ maxWidth: '75%' })
        }
        .width('100%')
        .alignItems(VerticalAlign.Top)
      } else {
        // 助手消息
        Row() {
          if (this.currentAgent?.avatar) {
            Image(this.currentAgent.avatar)
              .width(36)
              .height(36)
              .borderRadius(18)
              .objectFit(ImageFit.Cover)
          } else {
            Circle()
              .width(36)
              .height(36)
              .fill('#007DFF')
          }
          
          Column() {
            if (msg.isLoading) {
              LoadingProgress()
                .width(24)
                .height(24)
                .color('#007DFF')
            } else {
              Text(msg.content)
                .fontSize(15)
                .lineHeight(22)
                .padding({ left: 14, right: 14, top: 10, bottom: 10 })
                .backgroundColor('#FFFFFF')
                .fontColor('#333333')
                .borderRadius({ topLeft: 16, topRight: 16, bottomLeft: 4, bottomRight: 16 })
                .constraintSize({ maxWidth: '75%' })
              
              // 知识来源
              if (msg.knowledgeHits && msg.knowledgeHits.length > 0) {
                Column() {
                  Text('知识来源')
                    .fontSize(12)
                    .fontColor('#999999')
                    .width('100%')
                    .margin({ bottom: 8 })
                  
                  ForEach(msg.knowledgeHits.slice(0, 3), (hit: KnowledgeHit, index: number) => {
                    Row() {
                      Text(`${index + 1}. ${hit.source}`)
                        .fontSize(12)
                        .fontColor('#007DFF')
                        .maxLines(1)
                        .textOverflow({ overflow: TextOverflow.Ellipsis })
                      
                      Blank()
                      
                      Text(`相似度: ${(hit.score * 100).toFixed(0)}%`)
                        .fontSize(11)
                        .fontColor('#999999')
                    }
                    .width('100%')
                    .padding({ left: 10, right: 10, top: 8, bottom: 8 })
                    .backgroundColor('#F5F8FF')
                    .borderRadius(8)
                    .margin({ bottom: index < 2 ? 6 : 0 })
                  })
                }
                .width('100%')
                .padding({ left: 10, right: 10, top: 12, bottom: 12 })
                .backgroundColor('#FAFAFA')
                .borderRadius(12)
                .margin({ top: 10 })
              }
            }
          }
          .alignItems(HorizontalAlign.Start)
          .margin({ left: 12 })
          .layoutWeight(1)
        }
        .width('100%')
        .alignItems(VerticalAlign.Top)
      }
    }
    .width('100%')
    .margin({ bottom: 16 })
  }
}
