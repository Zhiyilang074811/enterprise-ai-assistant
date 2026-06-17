import { ApiService } from '../services/ApiService';
import { Constants } from '../utils/Constants';
import { preferences } from '@kit.ArkData';
import { router } from '@kit.ArkUI';

/**
 * 登录页面
 */
@Entry
@Component
struct Index {
  @State phone: string = '';
  @State password: string = '';
  @State isLoading: boolean = false;
  @State errorMessage: string = '';
  
  private apiService: ApiService = ApiService.getInstance();
  
  /**
   * 登录处理
   */
  async handleLogin(): Promise<void> {
    if (!this.phone.trim() || !this.password.trim()) {
      this.errorMessage = '请输入手机号和密码';
      return;
    }
    
    this.isLoading = true;
    this.errorMessage = '';
    
    try {
      const response = await this.apiService.login(this.phone.trim(), this.password.trim());
      
      if (response.ok) {
        // 保存登录信息
        await this.saveLoginInfo(response);
        
        // 跳转到聊天页面
        router.replaceUrl({
          url: 'pages/Chat'
        });
      } else {
        this.errorMessage = response.msg || '登录失败';
      }
    } catch (error) {
      this.errorMessage = '网络错误，请稍后重试';
      console.error('Login error:', error);
    } finally {
      this.isLoading = false;
    }
  }
  
  /**
   * 保存登录信息到本地存储
   */
  async saveLoginInfo(response: any): Promise<void> {
    try {
      const prefs = preferences.getPreferencesSync(getContext(), { name: 'app_prefs' });
      
      await prefs.put(Constants.KEY_PHONE, response.phone || '');
      await prefs.put(Constants.KEY_TENANT_ID, response.tenant_id || Constants.DEFAULT_TENANT_ID);
      await prefs.flush();
    } catch (error) {
      console.error('Save login info error:', error);
    }
  }
  
  build() {
    Column() {
      // Logo区域
      Column() {
        Image($r('app.media.ic_launcher'))
          .width(100)
          .height(100)
          .objectFit(ImageFit.Contain)
          .borderRadius(20)
        
        Text('企业智能助手')
          .fontSize(28)
          .fontWeight(FontWeight.Bold)
          .margin({ top: 16 })
        
        Text('多租户企业级问答平台')
          .fontSize(14)
          .fontColor('#666666')
          .margin({ top: 8 })
      }
      .width('100%')
      .padding({ top: 80, bottom: 60 })
      
      // 表单区域
      Column() {
        // 手机号输入
        TextInput({ text: this.phone, placeholder: '请输入手机号' })
          .type(InputType.PhoneNumber)
          .width('100%')
          .height(50)
          .backgroundColor('#F5F5F5')
          .borderRadius(10)
          .padding({ left: 16, right: 16 })
          .onChange((value: string) => {
            this.phone = value;
            this.errorMessage = '';
          })
        
        // 密码输入
        TextInput({ text: this.password, placeholder: '请输入密码' })
          .type(InputType.Password)
          .width('100%')
          .height(50)
          .backgroundColor('#F5F5F5')
          .borderRadius(10)
          .padding({ left: 16, right: 16 })
          .margin({ top: 16 })
          .onChange((value: string) => {
            this.password = value;
            this.errorMessage = '';
          })
        
        // 错误提示
        if (this.errorMessage) {
          Text(this.errorMessage)
            .fontSize(14)
            .fontColor('#FF4444')
            .margin({ top: 12 })
        }
        
        // 登录按钮
        Button(this.isLoading ? '登录中...' : '登录')
          .width('100%')
          .height(50)
          .fontSize(16)
          .fontWeight(FontWeight.Medium)
          .borderRadius(25)
          .margin({ top: 24 })
          .enabled(!this.isLoading)
          .backgroundColor(this.isLoading ? '#CCCCCC' : '#007DFF')
          .onClick(() => {
            this.handleLogin();
          })
        
        // 测试账号提示
        Column() {
          Text('测试账号')
            .fontSize(12)
            .fontColor('#999999')
          
          Text('平台管理员: platform_admin / Platform@2026')
            .fontSize(11)
            .fontColor('#999999')
            .margin({ top: 4 })
          
          Text('租户管理员: tenant_admin / Tenant@2026')
            .fontSize(11)
            .fontColor('#999999')
            .margin({ top: 2 })
        }
        .margin({ top: 40 })
      }
      .width('100%')
      .padding({ left: 32, right: 32 })
      
      Blank()
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#FFFFFF')
  }
}
