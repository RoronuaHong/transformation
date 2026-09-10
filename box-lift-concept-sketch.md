# Urban Halo · 坐姿全向悬停

**唯一现行稿：** [`box-lift-sitting-concept.html`](box-lift-sitting-concept.html)（概念冻结 v1）

> ⚠ **尺寸基准待修订**：页面上的外形属于早期紧凑构型，经
> [`box-lift-sizing.html`](box-lift-sizing.html) 收敛分析证明**物理上不成立**（闭环发散）。
> 可行构型为 **A 系留版 ⌀2.24 m** 或 **B 混动版 ⌀3.84 m**；
> 现有图形仅代表操控方式与人机布局，**不代表最终尺寸**。

## 冻结要点

| In | Out |
|----|-----|
| 坐姿单人 · 护栏 · 约束 · 前挡 | 穿戴式 |
| 扁椭圆底 + 周向 6 涵道（脚下） | 站立托举 |
| 六向平移 + 转向（飞控） | 中心单喷 |
| 轮 = 地面/着陆 | 开放街道 |
| 异常缓降 | DIY / 制造参数 |

## 效果图（已对齐）

- `concept-renders/urban-halo-omni-occupied.jpg` 带人  
- `concept-renders/urban-halo-omni-empty.jpg` 空载  

旧穿戴/站立 HTML 已移除（若仍存在可删）。

## 电池调研

见 [`box-lift-battery-research.md`](box-lift-battery-research.md)：  
现状 ~250–300 Wh/kg 电芯级；固态/锂金属 400–500 仍偏试点；对紧凑载人悬停仍是第一物理瓶颈。

## 安全性（已加强）

见 [`box-lift-safety.md`](box-lift-safety.md)。

## 可行性分析

见 [`box-lift-feasibility.md`](box-lift-feasibility.md)。

## 投产判断

见 [`box-lift-production-readiness.md`](box-lift-production-readiness.md)。

## 载重 300 斤 · 手动人控

见 [`box-lift-how-to-fly-300jin.md`](box-lift-how-to-fly-300jin.md)。

## 验证路径与门禁（已完成）

见 [`box-lift-validation-path.html`](box-lift-validation-path.html)：
七阶段门禁（概念冻结 → 非载人台架 → 系留 → 无人包线 → 监管沟通 → 载人试验 → 小批量），
每阶段有进入门禁、通过判据与 No-Go；任一阶段不通过即回退，不得跳级。

## 人机界面简图（已完成）

见 [`box-lift-hmi.html`](box-lift-hmi.html)：
四态灯（可飞 / 注意 / 降落 / 禁飞）+ 实体急停六步逻辑 + 人的输入映射。

## 收敛速算（新增）

见 [`box-lift-sizing.html`](box-lift-sizing.html)：重量—功率—能量迭代求解器，判定闭环是否收敛。
**默认参数（6×0.5 m 纯电、150 kg、悬停 5 min）迭代发散 = 该构型物理上不成立**；
纯电需涵道直径 ≥ ~1.2 m 才收敛；保留紧凑外形的唯一路径是系留供电。

## 差距清单（已并入速算页）

A/B/C 三类差距与三条出路已整合进 [`box-lift-sizing.html`](box-lift-sizing.html)
（「差距清单」区块），不再单独维护 md。

## 下一步（剩余）

~~概念基线已闭合~~ → **修正**：外形 / 操控 / 安全 / 人机 / 验证路径已闭合，
但**物理闭环未闭合**（默认构型发散）。当前阻塞点：

1. 拍板任务剖面（飞多久 / 多远 / 几架次）
2. 用速算器扫参数定构型（盘面积 + 能源），使闭环收敛
3. 收敛后再进入台架 / 系留验证门禁
