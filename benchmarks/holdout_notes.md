# hold-out 期间发现、但**刻意不改**的问题

冻结中（commit 2cfd307）。这些等整场评分结束后再动——冻结期间改规则，
hold-out 就退化成开发集了。

## 1. validator 的门槛金额规则漏了同义词

规则 `threshold_read_as_count` 只认 `orden(es) de N`。第四个主播说的是
`pedidos de N`，同一个意思，规则抓不到：

    ES  en pedidos de 40 chicas
    ZH  40个家人们的需求          ← 满 40 美元的订单被读成 40 个人
    validator: 未标记

`una orden de` 那条**词表**条目也一样，只收 orden 不收 pedido。

## 2. 新主播有自己的称呼

`nenas`（第四个主播）不在摘除名单里。第五个主播大概率又是别的词——
这正是 tools/onboard_streamer.py 存在的理由，不是往名单里无限加。

## 3. validator 漏掉的第二类：价格被读成债务

    ES  te está quedando en 47 dólares
    ZH  你现在只欠了47美元，你应该还47美元啊
    validator: 未标记

数字 47 一个不少、货币单位也在，所有现有规则都过。但「共计 47 美元」变成
「欠 47 美元」会让中控完全误判。这类需要的是**关系检查**，不是数值保全。

## 更正：第 2 条严重错误是我判错的

`te está quedando en 47 dólares y tú debes 47` → 「你现在只欠了47美元」

复查原文：`tú debes 47` 就在 Whisper 的输出里。译文忠实于源文，是 **ASR 把话
听乱了**，不是翻译加料。

所以：
- 这一场的严重错误从 2 条改为 **1 条 ≈ 1.3%**
- 为它设计的 `invented_debt_relation` 规则**没有上线**——600 条标注集和
  1204 条真实译文上各标记 0 条，零证据，唯一动机案例还站不住
