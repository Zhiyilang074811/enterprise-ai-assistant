# HarmonyOS App 上架完整指南

## 上架前准备清单

### 1. 华为开发者账号
- 注册: https://developer.huawei.com
- 类型: 个人开发者 / 企业开发者
- 费用: ¥199/年
- 认证: 身份证或营业执照

### 2. 开发环境
- DevEco Studio 4.0+
- HarmonyOS SDK NEXT
- 签名证书 (.cer + .p7b)

### 3. 应用材料
- 应用图标: 108x108px, 512x512px
- 应用截图: 至少 2 张 (1080x2340px)
- 应用描述: 30字简短 + 5000字详细
- 隐私政策文档

### 4. 打包发布流程
1. DevEco Studio -> File -> Project Structure -> Signing Configs
2. 导入签名证书
3. Build -> Build Hap(s) -> 生成 .hap 文件
4. 登录华为开发者联盟 -> 我的应用 -> 应用上架
5. 上传 .hap 文件，填写信息，提交审核

### 5. 审核周期
- 通常 1-3 个工作日
- 确保隐私政策合规
- 确保无敏感权限滥用