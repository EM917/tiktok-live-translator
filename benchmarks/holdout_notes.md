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
