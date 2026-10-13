# OnceBack — LIVE_RUN（真实 Airwallex sandbox）

- 运行时间：2026-10-11 01:32–01:37（UTC+8）。这是 10-11 回滚后的重新运行（原始运行在 2026-10-10 23:44–23:50），fixtures 也是这次重新录制的（文件名沿用 `*_2026-10-10.jsonl`，测试按这个名字加载）
- 环境：`https://api.sandbox.airwallex.com/api/v1`（`.env` 里的 `AIRWALLEX_BASE_URL=https://api-demo.airwallex.com` 登录返回 400 “API access has been disabled”，已改成 sandbox 域名）
- 命令：`cd onceback && ../.venv/bin/python -m onceback --live --record ../live/record.jsonl`（heuristic reader），以及 `--live --llm`（gemini-3.8-flash reader）
- 结果：6 个 demo case（C1、C2、C3、C4、C1-dup、C4-chargeback）全部真实跑通。另外新加的 C5（GB 银行退款 + 真实姓名核验）也跑通了。两次 live 运行的结局完全一致。
- 录制：59 条脱敏后的请求和响应存在 `onceback/tests/fixtures/live_run_2026-10-10.jsonl`，`tests/test_fixtures.py::test_replay_full_live_run` 会严格逐条重放（路径、参数、请求体都必须一致）。

## 每个 API 在 sandbox 上的真实表现

| 能力 | 真实接口 / 字段 | 结论 |
|---|---|---|
| 登录 | `POST /authentication/login` → `{token, expires_at}`（30 分钟） | 连续登录偶尔返回 400 “API access has been disabled”，过几秒重试就恢复；客户端加了退避重试和 token 缓存 |
| 收款建单 | `POST /pa/payment_intents/create` → `REQUIRES_PAYMENT_METHOD` + `client_secret` | ✅ |
| 测试卡付款 | 服务端 `/confirm` 直接传卡号 → 400 “This account is not enabled for native API access” | ❌ → **替代**：用无头 Chrome 打开 Airwallex.js Card element（官方 PCI 前端路径，env `demo`），填测试卡 4035 5010 0000 0008。付款后 `AUTHORIZED` → `SUCCEEDED`（`onceback/shopper/`） |
| 卡退款 | `POST /pa/refunds/create` → `RECEIVED`，再 GET → `ACCEPTED` | ✅ |
| 争议期间退款 | 争议开着或已 accept 时都返回 400 `amount_above_limit` “The operating amount X is above the limit 0.00.” | ✅ agent 把这个错误识别成“有争议”，转去 accept |
| request_id 重放 | 同一个 request_id 再发 → 400 `duplicate_request`（**不会**返回原对象） | ✅ agent 用 `GET /pa/refunds?payment_intent_id=` 按 request_id 找回原退款并采纳，不会重发 |
| 模拟拒付 | `POST /simulation/pa/payment_disputes/create {payment_intent_id, amount, reason_code, stage, due_at}` → `REQUIRES_RESPONSE`，`reason.type` 例如 13.1=`PRODUCT_NOT_RECEIVED`，13.6=`CREDIT_NOT_PROCESSED` | ✅ |
| 接受拒付 | `POST /pa/payment_disputes/{id}/accept {request_id, reason: VALID_CUSTOMER_DISPUTE, accepted_by, description}` → `ACCEPTED` | ✅ C3 用这个；accept 之后再退款同样是 `amount_above_limit` |
| 证据上传 | `POST https://files.sandbox.airwallex.com/api/v1/files/upload`（multipart `file` + `notes`）→ `{file_id}` | ✅ 退款收据 JPG 由 `evidence.py` 生成 |
| 抗辩拒付 | `POST /pa/payment_disputes/{id}/challenge {request_id, challenged_by, reason: CUSTOMER_REFUNDED, product_type, product_description, supporting_documents.documents[{type: PRIMARY, file_ids, description}]}` → `CHALLENGED`，`reason.type=CREDIT_NOT_PROCESSED` | ✅ C4-chargeback 用这个 |
| 账户核验（GB） | `POST /beneficiaries/verify_account` → `VERIFIED` + `FULL_MATCH` / `PARTIAL_MATCH` / `NOT_MATCHED`（sandbox 账户 040568/10006132，户名 “Betty Morris”） | ✅ 枚举值是 `NOT_MATCHED`，不是之前假设的 `NO_MATCH`，已修正 |
| 账户核验（JP） | 400 `transfer_method_unsupported` | ❌ → **替代**：记为 `CANNOT_VERIFY`，规则强制人工确认收款人后才付款。真实姓名匹配放在 C5（GB）上展示 |
| 收款人（JP LOCAL） | 必须带 `bank_account_category`（Savings/Checking）、`branch_code`、`first_name`/`last_name`、地址；返回 `{id, beneficiary}`，路由类型被规范成 `zengin_code` | ✅ |
| FX | `POST /fx/quotes/create` → `{quote_id, client_rate (USDJPY 157.22), sell_amount, usage: MULTI_USE}`；`/fx/conversions/create` → `{conversion_id, status: SETTLED}` | ✅ |
| 付款到日本银行 | `POST /transfers/create` → `SCHEDULED`；模拟 `SENT` → `PAID`。模拟 `PROCESSING` 返回 500 `operation_failed` | ✅（跳过 PROCESSING） |
| 余额 | `GET /balances/current`：sandbox 预置了 1000 万 USD/EUR/GBP/SGD/CNY，没有 JPY，所以 C2 每次都会真的换汇 | ✅ |

## 替代方案和诚实说明

1. **付款用 Airwallex.js，不用服务端 confirm**：这个账户没有开 native card API。浏览器路径本来就是 PCI 合规的标准做法，卡号只进 Airwallex 的 iframe。
2. **“214 天前的付款”来自商户订单记录**：sandbox 不能回填付款日期，PaymentIntent 都是当天建的。卡通道是否关闭由订单日期判断。真实生产里看的是同一个字段（订单或付款日期）。
3. **JP 没有姓名核验**：Airwallex 对 JP 不提供 verify_account，所以按“无法核验 → 人工确认”处理。真实的姓名匹配（PARTIAL_MATCH → 绑定 hash 后人工批准）在 C5 的 GB 账户上展示。
4. **审批人是脚本**：demo 里的审批人只批准 trace 中显示的 binding（金额、币种、通道、收款账户指纹、核验结果）。Web 控制台的审批卡还没做。
5. **拒付是模拟出来的**：用的是 sandbox simulation API。生产里这些是 `payment_dispute.*` webhook，目前没有接 webhook。
6. **C3 的“竞态”**：用 hook 在退款前一刻模拟拒付到达，agent 在出钱前的复核里发现了它。复核之后、退款之前才到达的拒付，由 `amount_above_limit` 回退路径处理（mock 测试覆盖，真实错误码已录制）。
7. **C4 是部分退款后的拒付**：Airwallex 自己只会在“已全额退款”时自动抗辩，部分退款明确不自动处理（见其 dispute 文档）。OnceBack 处理的正好是这个空档：退了 £576，拒付 £640。
8. **每次 live 运行使用新的 tag**：request_id 在 sandbox 里保留，重复跑会撞 `duplicate_request`。同一次运行内的幂等性仍然由 C1-dup 和账本体现。

## 运行记录 1：heuristic reader（重放测试用的就是这次）

```
mode=LIVE sandbox https://api.sandbox.airwallex.com/api/v1 tag=live-2ad888ae reader=heuristic

  setup: ORD-1001 120.00 EUR card ****0008 -> int_sgpvszg25hn369nlxlv SUCCEEDED
  setup: ORD-1002 48,000.00 JPY card ****0008 -> int_sgpvszg25hn369rfx7i SUCCEEDED
  setup: ORD-1003 89.00 USD card ****0008 -> int_sgpvszg25hn369upc2h SUCCEEDED
  setup: ORD-1004 640.00 GBP card ****0008 -> int_sgpvvnhskhn369y9cbo SUCCEEDED
  setup: ORD-1005 75.00 GBP card ****0008 -> int_sgpvvnhskhn36a1j1ze SUCCEEDED

== case C1 ==
  [GOAL    ] resolve refund request on ORD-1001 (120.00 EUR, paid 2026-09-28, customer Léa Moreau, FR, payment int_sgpvszg25hn369nlxlv)
  [READ    ] heuristic: intent=damaged evidence=['attachment:lamp_crack.jpg'] injection=False cites=['Bonjour, the ceramic lamp arrived cracked down the side.']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REFUND_CARD 120.00 EUR: damaged: full order value; payment is 12d old, card refund rail open (<= 180d)
  [CHECK   ] pre-refund re-check: no chargeback; sending refund
  [ACT     ] card refund rfd_sgpvszg25hn36a5c5pw_9nlxlv 120.00 EUR -> ACCEPTED
  => REFUNDED_TO_CARD 

== case C2 ==
  [GOAL    ] resolve refund request on ORD-1002 (48,000.00 JPY, paid 2026-03-10, customer Ken Tanaka, JP, payment int_sgpvszg25hn369rfx7i)
  [READ    ] heuristic: intent=wrong_item evidence=[] injection=False cites=['The preorder finally came but you sent me the wrong colour (grey, I ordered navy).']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REQUEST_BANK_DETAILS 48,000.00 JPY [needs human approval]: wrong_item: full order value; payment is 214d old, past the 180d card refund limit; refund must go out as a bank payout; ~USD 320.16 above auto-approve limit USD 150
  [NEW_INFO] verify_account(JP): CANNOT_VERIFY / None (bank holder '-', buyer 'Ken Tanaka')
  [REVISED ] PAYOUT_BANK 48,000.00 JPY [needs human approval]: wrong_item: full order value; payment is 214d old, past the 180d card refund limit; refund must go out as a bank payout; ~USD 320.16 above auto-approve limit USD 150; name check unavailable for this corridor (CANNOT_VERIFY); a person must confirm the payee
  [APPROVAL] asking a person to approve {'order_id': 'ORD-1002', 'payment_intent_id': 'int_sgpvszg25hn369rfx7i', 'amount': 48000, 'currency': 'JPY', 'rail': 'PAYOUT_BANK', 'beneficiary_account': 'f1923b5e0425', 'beneficiary': 'Ken Tanaka · JP 0001 ****4567', 'name_check': 'CANNOT_VERIFY'} (binding 9e1107f94d61b169)
  [APPROVAL] approved
  [ACT     ] FX: bought 48,000.00 JPY for 305.30 USD at 157.221379 (quote ca044000-bdd1-3140-a979-b7bf27f7b706, conversion 59f219c1-0dc6-46ce-bce3-da548b8f1547 SETTLED)
  [ACT     ] beneficiary 52879d6a-50dd-4e29-a18e-88004c361fd7; payout 65f0a478-69aa-436d-a8ef-c2e67e173c79 48,000.00 JPY via LOCAL -> SCHEDULED
  [RESULT  ] payout 65f0a478-69aa-436d-a8ef-c2e67e173c79 PAID
  => REFUNDED_TO_BANK 

== case C3 ==
  [GOAL    ] resolve refund request on ORD-1003 (89.00 USD, paid 2026-09-20, customer Sam Okafor, US, payment int_sgpvszg25hn369upc2h)
  [READ    ] heuristic: intent=not_received evidence=[] injection=False cites=['My order never arrived.']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REFUND_CARD 89.00 USD: not_received: full order value; payment is 20d old, card refund rail open (<= 180d)
  [NEW_INFO] pre-refund re-check: chargeback dst_sgpvqsbhzhn36ad18gl 89.00 USD PRODUCT_NOT_RECEIVED (13.1) is open on int_sgpvszg25hn369upc2h
  [REVISED ] ACCEPT_DISPUTE 89.00 USD: chargeback opened before our refund went out; accepting returns the money through the scheme; refunding as well would pay twice; refund cancelled
  [ACT     ] accepted chargeback dst_sgpvqsbhzhn36ad18gl -> ACCEPTED; no refund sent (no double pay)
  => ACCEPT_DISPUTE 

== case C4 ==
  [GOAL    ] resolve refund request on ORD-1004 (640.00 GBP, paid 2026-10-01, customer Olivia Hart, GB, payment int_sgpvvnhskhn369y9cbo)
  [READ    ] heuristic: intent=changed_mind evidence=[] injection=True cites=['I changed my mind about the coat, returning it unworn.']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REFUND_CARD 576.00 GBP [needs human approval]: message contains instructions aimed at the agent; treated as data, flagged for review; changed-mind return: 10% restocking fee (64.00 GBP); customer asked for 6,400.00; ignored, amount comes from the order; payment is 9d old, card refund rail open (<= 180d); ~USD 729.22 above auto-approve limit USD 150
  [APPROVAL] asking a person to approve {'order_id': 'ORD-1004', 'payment_intent_id': 'int_sgpvvnhskhn369y9cbo', 'amount': 576.0, 'currency': 'GBP', 'rail': 'REFUND_CARD'} (binding 2f789fe4f3f55e43)
  [APPROVAL] approved
  [CHECK   ] pre-refund re-check: no chargeback; sending refund
  [ACT     ] card refund rfd_sgpvszg25hn36aeion6_9y9cbo 576.00 GBP -> ACCEPTED
  => REFUNDED_TO_CARD 

== case C1-dup ==
  [GOAL    ] resolve refund request on ORD-1001 (120.00 EUR, paid 2026-09-28, customer Léa Moreau, FR, payment int_sgpvszg25hn369nlxlv)
  [READ    ] heuristic: intent=damaged evidence=['attachment:lamp_crack.jpg'] injection=False cites=['The lamp arrived cracked, I want my refund.']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] DECLINE: nothing left to refund on this payment; it was already returned in full
  => DECLINE 

== case C5 ==
  [GOAL    ] resolve refund request on ORD-1005 (75.00 GBP, paid 2026-02-14, customer Betty Morriss, GB, payment int_sgpvvnhskhn36a1j1ze)
  [READ    ] heuristic: intent=not_received evidence=[] injection=False cites=['My parcel from February never arrived, the courier says it was lost.']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REQUEST_BANK_DETAILS 75.00 GBP: not_received: full order value; payment is 238d old, past the 180d card refund limit; refund must go out as a bank payout
  [NEW_INFO] verify_account(GB): VERIFIED / PARTIAL_MATCH (bank holder 'Betty Morris', buyer 'Betty Morriss')
  [REVISED ] PAYOUT_BANK 75.00 GBP [needs human approval]: not_received: full order value; payment is 238d old, past the 180d card refund limit; refund must go out as a bank payout; account verified, holder name only partially matches the buyer
  [APPROVAL] asking a person to approve {'order_id': 'ORD-1005', 'payment_intent_id': 'int_sgpvvnhskhn36a1j1ze', 'amount': 75.0, 'currency': 'GBP', 'rail': 'PAYOUT_BANK', 'beneficiary_account': '13bb556dae48', 'beneficiary': 'Betty Morris · GB 040568 ****6132', 'name_check': 'PARTIAL_MATCH'} (binding 3c4b20e746dd720f)
  [APPROVAL] approved
  [CHECK   ] wallet holds 9,999,625.00 GBP; no conversion needed
  [ACT     ] beneficiary 96e27657-c4db-4300-b258-3ef0d1f991d9; payout 30ef441c-5690-438f-9767-5d91f10e7bf8 75.00 GBP via LOCAL -> SCHEDULED
  [RESULT  ] payout 30ef441c-5690-438f-9767-5d91f10e7bf8 PAID
  => REFUNDED_TO_BANK 

== case C4-chargeback ==
  [NEW_INFO] chargeback dst_sgpvqsbhzhn36akj0ni 640.00 GBP CREDIT_NOT_PROCESSED (13.6) stage CHARGEBACK on int_sgpvvnhskhn369y9cbo
  [REVISED ] CHALLENGE_DISPUTE 640.00 GBP: refund of 576.00 GBP already completed; challenge with 'credit processed' evidence instead of accepting a second debit
  [ACT     ] uploaded receipt NjEzYzI0NjAtMWQw…; challenged dst_sgpvqsbhzhn36akj0ni reason CUSTOMER_REFUNDED -> CHALLENGED
  => CHALLENGE_DISPUTE 

== ledger (money returned per payment; must never exceed captured) ==
  ORD-1001 EUR: captured 120.00 refunded 120.00 chargeback 0.00 -> returned 120.00  refs=['rfd_sgpvszg25hn36a5c5pw_9nlxlv']
  ORD-1002 JPY: captured 48,000.00 refunded 48,000.00 chargeback 0.00 -> returned 48,000.00  refs=['65f0a478-69aa-436d-a8ef-c2e67e173c79']
  ORD-1003 USD: captured 89.00 refunded 0.00 chargeback 89.00 -> returned 89.00  refs=[]
  ORD-1004 GBP: captured 640.00 refunded 576.00 chargeback 0.00 -> returned 576.00  refs=['rfd_sgpvszg25hn36aeion6_9y9cbo']
  ORD-1005 GBP: captured 75.00 refunded 75.00 chargeback 0.00 -> returned 75.00  refs=['30ef441c-5690-438f-9767-5d91f10e7bf8']

== Airwallex view (read back from the sandbox) ==
  int_sgpvszg25hn369nlxlv SUCCEEDED captured 120 disputes []
  int_sgpvszg25hn369rfx7i SUCCEEDED captured 48000 disputes []
  int_sgpvszg25hn369upc2h SUCCEEDED captured 89 disputes [('dst_sgpvqsbhzhn36ad18gl', 'ACCEPTED')]
  int_sgpvvnhskhn369y9cbo SUCCEEDED captured 640 disputes [('dst_sgpvqsbhzhn36akj0ni', 'CHALLENGED')]
  int_sgpvvnhskhn36a1j1ze SUCCEEDED captured 75 disputes []
```

## 运行记录 2：LLM reader（gemini-3.8-flash）

```
mode=LIVE sandbox https://api.sandbox.airwallex.com/api/v1 tag=live-489725f0 reader=llm

  setup: ORD-1001 120.00 EUR card ****0008 -> int_sgpvszg25hn36athsvg SUCCEEDED
  setup: ORD-1002 48,000.00 JPY card ****0008 -> int_sgpvvnhskhn36awyhj4 SUCCEEDED
  setup: ORD-1003 89.00 USD card ****0008 -> int_sgpvszg25hn36b025ea SUCCEEDED
  setup: ORD-1004 640.00 GBP card ****0008 -> int_sgpvszg25hn36b2vt4l SUCCEEDED
  setup: ORD-1005 75.00 GBP card ****0008 -> int_sgpvvnhskhn36b5xek2 SUCCEEDED

== case C1 ==
  [GOAL    ] resolve refund request on ORD-1001 (120.00 EUR, paid 2026-09-28, customer Léa Moreau, FR, payment int_sgpvszg25hn36athsvg)
  [READ    ] llm:gemini-3.8-flash: intent=damaged evidence=['photo'] injection=False cites=['arrived cracked down the side']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REFUND_CARD 120.00 EUR: damaged: full order value; payment is 12d old, card refund rail open (<= 180d)
  [CHECK   ] pre-refund re-check: no chargeback; sending refund
  [ACT     ] card refund rfd_sgpvvnhskhn36bdshxf_athsvg 120.00 EUR -> ACCEPTED
  => REFUNDED_TO_CARD 

== case C2 ==
  [GOAL    ] resolve refund request on ORD-1002 (48,000.00 JPY, paid 2026-03-10, customer Ken Tanaka, JP, payment int_sgpvvnhskhn36awyhj4)
  [READ    ] llm:gemini-3.8-flash: intent=wrong_item evidence=[] injection=False cites=['sent me the wrong colour (grey, I ordered navy)']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REQUEST_BANK_DETAILS 48,000.00 JPY [needs human approval]: wrong_item: full order value; payment is 214d old, past the 180d card refund limit; refund must go out as a bank payout; ~USD 320.16 above auto-approve limit USD 150
  [NEW_INFO] verify_account(JP): CANNOT_VERIFY / None (bank holder '-', buyer 'Ken Tanaka')
  [REVISED ] PAYOUT_BANK 48,000.00 JPY [needs human approval]: wrong_item: full order value; payment is 214d old, past the 180d card refund limit; refund must go out as a bank payout; ~USD 320.16 above auto-approve limit USD 150; name check unavailable for this corridor (CANNOT_VERIFY); a person must confirm the payee
  [APPROVAL] asking a person to approve {'order_id': 'ORD-1002', 'payment_intent_id': 'int_sgpvvnhskhn36awyhj4', 'amount': 48000, 'currency': 'JPY', 'rail': 'PAYOUT_BANK', 'beneficiary_account': 'f1923b5e0425', 'beneficiary': 'Ken Tanaka · JP 0001 ****4567', 'name_check': 'CANNOT_VERIFY'} (binding 9d9540fd479e285c)
  [APPROVAL] approved
  [ACT     ] FX: bought 48,000.00 JPY for 305.30 USD at 157.221379 (quote bb4fd332-78d8-3a5e-a487-c802991341be, conversion c4c3f089-6e60-4910-ae8f-b0cda1517046 SETTLED)
  [ACT     ] beneficiary 49773ad2-04cd-48bc-8e20-894470a5125d; payout c61b5f03-691b-4f46-af7d-3cc2272702cb 48,000.00 JPY via LOCAL -> SCHEDULED
  [RESULT  ] payout c61b5f03-691b-4f46-af7d-3cc2272702cb PAID
  => REFUNDED_TO_BANK 

== case C3 ==
  [GOAL    ] resolve refund request on ORD-1003 (89.00 USD, paid 2026-09-20, customer Sam Okafor, US, payment int_sgpvszg25hn36b025ea)
  [READ    ] llm:gemini-3.8-flash: intent=not_received evidence=['tracking'] injection=False cites=['My order never arrived.']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REFUND_CARD 89.00 USD: not_received: full order value; payment is 20d old, card refund rail open (<= 180d)
  [NEW_INFO] pre-refund re-check: chargeback dst_sgpvqsbhzhn36bqii8s 89.00 USD PRODUCT_NOT_RECEIVED (13.1) is open on int_sgpvszg25hn36b025ea
  [REVISED ] ACCEPT_DISPUTE 89.00 USD: chargeback opened before our refund went out; accepting returns the money through the scheme; refunding as well would pay twice; refund cancelled
  [ACT     ] accepted chargeback dst_sgpvqsbhzhn36bqii8s -> ACCEPTED; no refund sent (no double pay)
  => ACCEPT_DISPUTE 

== case C4 ==
  [GOAL    ] resolve refund request on ORD-1004 (640.00 GBP, paid 2026-10-01, customer Olivia Hart, GB, payment int_sgpvszg25hn36b2vt4l)
  [READ    ] llm:gemini-3.8-flash: intent=changed_mind evidence=[] injection=True cites=['I changed my mind about the coat']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REFUND_CARD 576.00 GBP [needs human approval]: message contains instructions aimed at the agent; treated as data, flagged for review; changed-mind return: 10% restocking fee (64.00 GBP); customer asked for 6,400.00; ignored, amount comes from the order; payment is 9d old, card refund rail open (<= 180d); ~USD 729.22 above auto-approve limit USD 150
  [APPROVAL] asking a person to approve {'order_id': 'ORD-1004', 'payment_intent_id': 'int_sgpvszg25hn36b2vt4l', 'amount': 576.0, 'currency': 'GBP', 'rail': 'REFUND_CARD'} (binding 3cf7cb37b50d885a)
  [APPROVAL] approved
  [CHECK   ] pre-refund re-check: no chargeback; sending refund
  [ACT     ] card refund rfd_sgpvvnhskhn36bursy0_b2vt4l 576.00 GBP -> ACCEPTED
  => REFUNDED_TO_CARD 

== case C1-dup ==
  [GOAL    ] resolve refund request on ORD-1001 (120.00 EUR, paid 2026-09-28, customer Léa Moreau, FR, payment int_sgpvszg25hn36athsvg)
  [READ    ] llm:gemini-3.8-flash: intent=damaged evidence=['photo'] injection=False cites=['The lamp arrived cracked']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] DECLINE: nothing left to refund on this payment; it was already returned in full
  => DECLINE 

== case C5 ==
  [GOAL    ] resolve refund request on ORD-1005 (75.00 GBP, paid 2026-02-14, customer Betty Morriss, GB, payment int_sgpvvnhskhn36b5xek2)
  [READ    ] llm:gemini-3.8-flash: intent=not_received evidence=['courier statement'] injection=False cites=['parcel from February never arrived', 'courier says it was lost']
  [CHECK   ] no chargeback on this payment
  [PLAN    ] REQUEST_BANK_DETAILS 75.00 GBP: not_received: full order value; payment is 238d old, past the 180d card refund limit; refund must go out as a bank payout
  [NEW_INFO] verify_account(GB): VERIFIED / PARTIAL_MATCH (bank holder 'Betty Morris', buyer 'Betty Morriss')
  [REVISED ] PAYOUT_BANK 75.00 GBP [needs human approval]: not_received: full order value; payment is 238d old, past the 180d card refund limit; refund must go out as a bank payout; account verified, holder name only partially matches the buyer
  [APPROVAL] asking a person to approve {'order_id': 'ORD-1005', 'payment_intent_id': 'int_sgpvvnhskhn36b5xek2', 'amount': 75.0, 'currency': 'GBP', 'rail': 'PAYOUT_BANK', 'beneficiary_account': '13bb556dae48', 'beneficiary': 'Betty Morris · GB 040568 ****6132', 'name_check': 'PARTIAL_MATCH'} (binding 54e974f78fdc6fec)
  [APPROVAL] approved
  [CHECK   ] wallet holds 9,999,550.00 GBP; no conversion needed
  [ACT     ] beneficiary c9749d76-21be-4e18-99ee-0486fd5aff50; payout 1b781c0a-2427-4648-bfd7-182a6b048966 75.00 GBP via LOCAL -> SCHEDULED
  [RESULT  ] payout 1b781c0a-2427-4648-bfd7-182a6b048966 PAID
  => REFUNDED_TO_BANK 

== case C4-chargeback ==
  [NEW_INFO] chargeback dst_sgpvqsbhzhn36c7xi05 640.00 GBP CREDIT_NOT_PROCESSED (13.6) stage CHARGEBACK on int_sgpvszg25hn36b2vt4l
  [REVISED ] CHALLENGE_DISPUTE 640.00 GBP: refund of 576.00 GBP already completed; challenge with 'credit processed' evidence instead of accepting a second debit
  [ACT     ] uploaded receipt NjEzYzI0NjAtMWQw…; challenged dst_sgpvqsbhzhn36c7xi05 reason CUSTOMER_REFUNDED -> CHALLENGED
  => CHALLENGE_DISPUTE 

== ledger (money returned per payment; must never exceed captured) ==
  ORD-1001 EUR: captured 120.00 refunded 120.00 chargeback 0.00 -> returned 120.00  refs=['rfd_sgpvvnhskhn36bdshxf_athsvg']
  ORD-1002 JPY: captured 48,000.00 refunded 48,000.00 chargeback 0.00 -> returned 48,000.00  refs=['c61b5f03-691b-4f46-af7d-3cc2272702cb']
  ORD-1003 USD: captured 89.00 refunded 0.00 chargeback 89.00 -> returned 89.00  refs=[]
  ORD-1004 GBP: captured 640.00 refunded 576.00 chargeback 0.00 -> returned 576.00  refs=['rfd_sgpvvnhskhn36bursy0_b2vt4l']
  ORD-1005 GBP: captured 75.00 refunded 75.00 chargeback 0.00 -> returned 75.00  refs=['1b781c0a-2427-4648-bfd7-182a6b048966']

== Airwallex view (read back from the sandbox) ==
  int_sgpvszg25hn36athsvg SUCCEEDED captured 120 disputes []
  int_sgpvvnhskhn36awyhj4 SUCCEEDED captured 48000 disputes []
  int_sgpvszg25hn36b025ea SUCCEEDED captured 89 disputes [('dst_sgpvqsbhzhn36bqii8s', 'ACCEPTED')]
  int_sgpvszg25hn36b2vt4l SUCCEEDED captured 640 disputes [('dst_sgpvqsbhzhn36c7xi05', 'CHALLENGED')]
  int_sgpvvnhskhn36b5xek2 SUCCEEDED captured 75 disputes []
```
