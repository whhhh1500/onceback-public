# OnceBack 计划

## 一句话
OnceBack：跨境商户的退款 agent。每一笔退款只退一次，币种对、通道对，并且绝不和拒付重复。模型只负责读客户消息，金额、通道、审批和防重复都写在代码里，资金通过 Airwallex API 移动。

## 架构（已搭好，见 onceback/）
- `reader.py`：读客户消息（意图、引用证据、提示注入检测），可选 LLM，失败时退回规则读取。
- `policy.py`：纯代码策略：退货期、10% 退货手续费、自动批准上限、滥用规则、通道选择（卡退款 / 银行打款）、拒付应对、姓名核验结果的处理。
- `ledger.py`：每笔付款一个账本：已退、在途、拒付，保证“退回总额 ≤ 实收”；request_id 由业务键确定性生成。
- `agent.py`：读 → 决定 → 审批（绑定哈希）→ 执行 → 对账；动钱前重新检查争议。
- `airwallex.py`：类型化的 Airwallex 客户端；沙盒专用的模拟接口都放在 `Sim` 里。`mock.py` 是离线的沙盒镜像。

## 里程碑（时间均为 UTC+8）
| 日期 | 内容 | 谁 |
|---|---|---|
| 10-11 | 注册沙盒，拿到 Client ID 和 API Key 并写入 `onceback/.env` | **用户** |
| 10-11 ~ 10-13 | 接通 live：登录、建 USD Global Account、模拟入账；逐个验证 payment_intents/confirm、refunds、payment_disputes 列表和模拟、verify_account（沙盒的姓名匹配行为）、beneficiaries、fx、transfers 的真实字段，修正 `airwallex.py`（重点核对：dispute 模拟的 body、accept/challenge 的 reason 枚举、payment_intent 的 confirm 结构、JP 的 LOCAL 收款字段，必要时演示改用 GB 或 US 通道） | agent |
| 10-13 ~ 10-20 | HackerEarth 报名并提交 idea，**10-22 前完成** | **用户** |
| 10-14 ~ 10-24 | 一个简单 Web 看板（FastAPI + 单页）：案件队列、每案的 trace、审批卡片（显示绑定哈希）、账本恒等式；接 webhook（verify x-signature，按事件 id 去重） | agent |
| 10-25 ~ 11-05 | live 模式完整演示；补充"卡退款 FAILED 后自动切换到打款"的真实路径；接入 Claude（sonnet-4-6）作为第二读取器做交叉对照（可选） | agent |
| 11-05 ~ 11-10 | 写 5 分钟视频脚本和录屏素材；公开仓库，README 里写安装步骤 | agent 起草；**录音、上传由用户完成** |
| 11-12 | 在 HackerEarth 提交 zip、仓库链接和视频（截止时间 11-14 02:29 UTC+8） | **用户** |

## 风险
- （已验证）JP 的 verify_account 返回 `transfer_method_unsupported`，现在按 CANNOT_VERIFY 处理并要求人工确认；真实姓名匹配放在 C5 的 GB 通道上展示。
- （已验证）服务端卡 confirm 未开通 → 用 Airwallex.js 无头浏览器付款。如果评委要在自己机器上跑 live，需要 Chrome 和 playwright；mock 模式不需要。
- 撞题（2026-10-11 已核对 7 份竞品正文）：“不付两次”和拒付应答都很拥挤；和 Kept 重叠约三分之一。定位收紧为“退款执行”：卡通道关闭时改走核验过户名的银行打款、按原币退、和拒付共用一个预算。Track 改选 Kit 04。
- 沙盒里可能无法让卡退款主动失败，所以"卡通道关闭"的判断按支付时长写在策略里，这本身就是真实规则。
- 争议模拟的阶段限制（文档：在 PRE_CHARGEBACK 阶段创建会在同一秒自动接受；应在 RFI 或 CHARGEBACK 阶段创建）。

## 只有用户能做的事
1. 注册沙盒（邮箱、姓名、国家），拿到 API Key。沙盒不需要 KYC。
2. 用自己的账号在 HackerEarth 报名并提交 idea（实名资料）。
3. 公开 GitHub 仓库、录制并上传视频、点击最终提交。
4. 如果获奖：填税表、提供收款信息；必要时就中国居民领奖问题联系主办方。
