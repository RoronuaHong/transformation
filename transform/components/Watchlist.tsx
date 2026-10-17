"use client";

import React from "react";

/* ----------------------------------------------------------------------------
 * 加密货币观察列表看板（优化版）
 * - 布局/排版：统一卡片间距、表格右对齐数值(tnum)、语义化配色、图例、空状态
 * - 新增「原价」列：预测生成时刻的基准价，与 预测价 / 实际价 构成完整对比链
 * - 数据驱动：所有区块均通过 WatchlistProps 注入（含默认值），可直连后端 snapshots.db / watchlist。
 * -------------------------------------------------------------------------- */

type Dir = "强多" | "多" | "空" | "强空" | "平";
type Timeframe = "1h" | "4h" | "1d" | "1w";
type ResultState = "胜" | "负" | "平" | "待结算";

interface TfMetric {
  dir: Dir;
  price: number;
  rsi: number;
  adx: number;
  macd: number;
  vr: number;
}

interface CoinSnap {
  coin: string;
  metrics: Record<Timeframe, TfMetric>;
}

interface Signal {
  label: string;
  tone: "neutral" | "bear" | "bull";
}
type AnalysisMap = Record<string, Partial<Record<Timeframe, Signal[]>>>;

interface Prediction {
  coin: string;
  tf: Timeframe;
  bias: "看多" | "看空";
  /** 原价：预测生成时刻的基准价 */
  origPrice: number;
  predPrice: number;
  actualPrice: number | null;
  actualDir: Dir | null;
  result: ResultState;
  at: string;
}

/* ----------------------------- 示例数据 ----------------------------- */

const WATCHLIST = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TRX"];

const SNAPSHOTS: CoinSnap[] = [
  { coin: "BTC", metrics: {
    "1h": { dir: "强空", price: 81556.8, rsi: 46.5, adx: 8.0, macd: 39.97, vr: 0.15 },
    "4h": { dir: "强空", price: 81526.0, rsi: 43.5, adx: 47.6, macd: -12.84, vr: 0.14 },
    "1d": { dir: "空", price: 81657.14, rsi: 46.8, adx: 24.2, macd: -1019.84, vr: 0.13 },
    "1w": { dir: "多", price: 81657.14, rsi: 56.7, adx: 27.0, macd: 4521.38, vr: 0.74 },
  }},
  { coin: "ETH", metrics: {
    "1h": { dir: "多", price: 2435.0, rsi: 53.2, adx: 11.6, macd: 3.69, vr: 0.25 },
    "4h": { dir: "空", price: 2434.67, rsi: 46.4, adx: 28.3, macd: 7.2, vr: 0.2 },
    "1d": { dir: "空", price: 2432.79, rsi: 39.8, adx: 29.7, macd: -51.51, vr: 0.09 },
    "1w": { dir: "多", price: 2433.91, rsi: 53.7, adx: 21.5, macd: 157.09, vr: 0.81 },
  }},
  { coin: "BNB", metrics: {
    "1h": { dir: "强空", price: 721.45, rsi: 39.1, adx: 23.7, macd: 0.41, vr: 0.19 },
    "4h": { dir: "强空", price: 721.48, rsi: 38.4, adx: 38.4, macd: -0.02, vr: 0.21 },
    "1d": { dir: "空", price: 721.64, rsi: 39.9, adx: 24.6, macd: -13.36, vr: 0.19 },
    "1w": { dir: "多", price: 721.63, rsi: 53.5, adx: 25.4, macd: 36.16, vr: 0.57 },
  }},
  { coin: "SOL", metrics: {
    "1h": { dir: "多", price: 104.64, rsi: 48.1, adx: 11.6, macd: 0.12, vr: 0.21 },
    "4h": { dir: "强空", price: 104.65, rsi: 44.8, adx: 35.9, macd: 0.44, vr: 0.2 },
    "1d": { dir: "空", price: 105.03, rsi: 40.0, adx: 29.3, macd: -3.48, vr: 0.11 },
    "1w": { dir: "多", price: 104.98, rsi: 54.0, adx: 28.2, macd: 9.05, vr: 0.72 },
  }},
  { coin: "XRP", metrics: {
    "1h": { dir: "强空", price: 1.31, rsi: 42.7, adx: 25.1, macd: 0.0, vr: 0.16 },
    "4h": { dir: "强空", price: 1.31, rsi: 39.0, adx: 45.8, macd: -0.0, vr: 0.23 },
    "1d": { dir: "空", price: 1.31, rsi: 36.2, adx: 25.4, macd: -0.04, vr: 0.06 },
    "1w": { dir: "多", price: 1.31, rsi: 47.5, adx: 26.3, macd: 0.09, vr: 0.69 },
  }},
  { coin: "DOGE", metrics: {
    "1h": { dir: "强空", price: 0.08, rsi: 44.7, adx: 23.0, macd: 0.0, vr: 0.29 },
    "4h": { dir: "强空", price: 0.08, rsi: 39.1, adx: 59.2, macd: 0.0, vr: 0.32 },
    "1d": { dir: "空", price: 0.08, rsi: 37.0, adx: 23.9, macd: -0.0, vr: 0.14 },
    "1w": { dir: "空", price: 0.08, rsi: 44.1, adx: 21.5, macd: 0.01, vr: 0.77 },
  }},
  { coin: "ADA", metrics: {
    "1h": { dir: "强空", price: 0.22, rsi: 37.2, adx: 28.5, macd: -0.0, vr: 0.29 },
    "4h": { dir: "强空", price: 0.22, rsi: 38.4, adx: 34.4, macd: 0.0, vr: 0.29 },
    "1d": { dir: "空", price: 0.22, rsi: 42.3, adx: 21.6, macd: -0.01, vr: 0.09 },
    "1w": { dir: "多", price: 0.22, rsi: 47.7, adx: 23.3, macd: 0.03, vr: 0.6 },
  }},
  { coin: "AVAX", metrics: {
    "1h": { dir: "空", price: 9.97, rsi: 45.1, adx: 13.5, macd: 0.0, vr: 0.39 },
    "4h": { dir: "空", price: 9.97, rsi: 45.3, adx: 24.2, macd: 0.05, vr: 0.42 },
    "1d": { dir: "空", price: 10.01, rsi: 48.1, adx: 25.3, macd: -0.41, vr: 0.11 },
    "1w": { dir: "多", price: 10.01, rsi: 54.4, adx: 32.4, macd: 1.36, vr: 0.78 },
  }},
  { coin: "LINK", metrics: {
    "1h": { dir: "强空", price: 12.2, rsi: 34.9, adx: 27.5, macd: -0.01, vr: 0.36 },
    "4h": { dir: "强空", price: 12.2, rsi: 39.9, adx: 27.5, macd: 0.0, vr: 0.35 },
    "1d": { dir: "空", price: 12.28, rsi: 42.7, adx: 24.8, macd: -0.44, vr: 0.09 },
    "1w": { dir: "多", price: 12.28, rsi: 55.2, adx: 32.0, macd: 1.02, vr: 0.62 },
  }},
  { coin: "TRX", metrics: {
    "1h": { dir: "强多", price: 0.34, rsi: 66.4, adx: 26.7, macd: 0.0, vr: 1.95 },
    "4h": { dir: "多", price: 0.34, rsi: 63.3, adx: 18.9, macd: 0.0, vr: 0.58 },
    "1d": { dir: "多", price: 0.34, rsi: 54.1, adx: 10.7, macd: 0.0, vr: 0.22 },
    "1w": { dir: "空", price: 0.34, rsi: 53.5, adx: 14.5, macd: -0.0, vr: 0.85 },
  }},
];

const ANALYSIS: AnalysisMap = {
  BNB: { "4h": [{ label: "MACD死叉", tone: "bear" }, { label: "顶背离", tone: "bear" }] },
  XRP: { "4h": [{ label: "顶背离", tone: "bear" }] },
};

const PREDICTIONS: Prediction[] = [
  { coin: "BTC", tf: "4h", bias: "看空", origPrice: 81580.0, predPrice: 81547.6, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:23:05" },
  { coin: "BNB", tf: "4h", bias: "看空", origPrice: 722.1, predPrice: 721.29, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:23:05" },
  { coin: "BTC", tf: "1h", bias: "看多", origPrice: 81550.0, predPrice: 81642.3, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:06:50" },
  { coin: "ETH", tf: "1h", bias: "看多", origPrice: 2433.0, predPrice: 2440.25, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:06:33" },
  { coin: "SOL", tf: "1h", bias: "看多", origPrice: 104.5, predPrice: 104.87, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:06:33" },
  { coin: "ADA", tf: "1h", bias: "看空", origPrice: 0.223, predPrice: 0.22, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:06:33" },
  { coin: "LINK", tf: "1h", bias: "看空", origPrice: 12.28, predPrice: 12.22, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:06:33" },
  { coin: "TRX", tf: "1h", bias: "看多", origPrice: 0.339, predPrice: 0.34, actualPrice: null, actualDir: null, result: "待结算", at: "2026/10/17 19:06:33" },
  { coin: "ETH", tf: "1h", bias: "看多", origPrice: 2430.5, predPrice: 2433.86, actualPrice: 2440.19, actualDir: "多", result: "胜", at: "2026/10/17 18:05:54" },
  { coin: "DOGE", tf: "1h", bias: "看空", origPrice: 0.082, predPrice: 0.08, actualPrice: 0.08, actualDir: "多", result: "负", at: "2026/10/17 18:05:54" },
  { coin: "ADA", tf: "1h", bias: "看空", origPrice: 0.223, predPrice: 0.22, actualPrice: 0.22, actualDir: "多", result: "负", at: "2026/10/17 18:05:54" },
  { coin: "LINK", tf: "1h", bias: "看空", origPrice: 12.25, predPrice: 12.22, actualPrice: 12.22, actualDir: "平", result: "平", at: "2026/10/17 18:05:54" },
  { coin: "TRX", tf: "1h", bias: "看多", origPrice: 0.338, predPrice: 0.34, actualPrice: 0.34, actualDir: "多", result: "胜", at: "2026/10/17 18:05:54" },
  { coin: "ETH", tf: "1h", bias: "看多", origPrice: 2429.0, predPrice: 2432.86, actualPrice: 2436.97, actualDir: "多", result: "胜", at: "2026/10/17 17:37:14" },
  { coin: "SOL", tf: "1h", bias: "看多", origPrice: 104.7, predPrice: 104.96, actualPrice: 104.85, actualDir: "空", result: "负", at: "2026/10/17 17:37:14" },
];

const WIN_RATE = { overall: 48.4, win: 30, loss: 32, total: 62 };
const WIN_BY_TF: { tf: Timeframe; rate: number; n: number }[] = [
  { tf: "1d", rate: 83.3, n: 6 },
  { tf: "1h", rate: 43.4, n: 53 },
  { tf: "4h", rate: 66.7, n: 3 },
];

/* ----------------------------- 工具函数 ----------------------------- */

function fmtPrice(n: number): string {
  const d = n >= 1 ? 2 : 4;
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

const TFS: Timeframe[] = ["1h", "4h", "1d", "1w"];

function dirClassName(dir: Dir): string {
  const color =
    dir === "强多" || dir === "多"
      ? "text-chart-2"
      : dir === "强空" || dir === "空"
      ? "text-chart-4"
      : "text-muted-foreground";
  const weight = dir === "强多" || dir === "强空" ? "font-bold" : "font-medium";
  return `${color} ${weight}`;
}

/* ----------------------------- 小组件 ----------------------------- */

function DirectionBadge({ dir }: { dir: Dir }) {
  return <span className={`text-sm tnum ${dirClassName(dir)}`}>{dir}</span>;
}

function ResultBadge({ result }: { result: ResultState }) {
  const cls =
    result === "胜"
      ? "bg-chart-2/15 text-chart-2"
      : result === "负"
      ? "bg-chart-4/15 text-chart-4"
      : result === "平"
      ? "text-muted-foreground"
      : "bg-secondary text-muted-foreground";
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${cls}`}>{result}</span>
  );
}

function SignalBadge({ s }: { s: Signal }) {
  const cls =
    s.tone === "bear"
      ? "bg-chart-4/15 text-chart-4"
      : s.tone === "bull"
      ? "bg-chart-2/15 text-chart-2"
      : "bg-secondary text-muted-foreground";
  return <span className={`rounded px-1.5 py-0.5 text-xs ${cls}`}>{s.label}</span>;
}

function SectionCard({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="mb-4 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

function ToolbarButton({
  label,
  primary,
}: {
  label: string;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      className={
        primary
          ? "h-8 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground transition-colors hover:opacity-90"
          : "h-8 rounded-md border border-input px-3 text-xs transition-colors hover:bg-accent hover:text-accent-foreground"
      }
    >
      {label}
    </button>
  );
}

function CompareCard({ tf }: { tf: Timeframe }) {
  return (
    <div className="rounded-lg border border-border/60 bg-background/40 p-3">
      <p className="mb-2 text-xs font-medium text-muted-foreground">{tf}（0 币，按动量排序）</p>
      <p className="text-[11px] text-muted-foreground">需 ≥2 次快照才能比对（该周期尚未记录满 2 次）</p>
    </div>
  );
}

/* ----------------------------- 主组件 ----------------------------- */

export interface WatchlistProps {
  watchlist?: string[];
  snapshots?: CoinSnap[];
  analysis?: AnalysisMap;
  predictions?: Prediction[];
  winRate?: { overall: number; win: number; loss: number; total: number };
  winByTf?: { tf: Timeframe; rate: number; n: number }[];
}

export default function Watchlist({
  watchlist = WATCHLIST,
  snapshots = SNAPSHOTS,
  analysis = ANALYSIS,
  predictions = PREDICTIONS,
  winRate = WIN_RATE,
  winByTf = WIN_BY_TF,
}: WatchlistProps = {}) {
  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6">
      {/* 头部 */}
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">观察列表</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            10 大热门币（可增删）· 定时记录 1h / 4h / 1d / 1w 指标 · 每日自动比对方向变化与动量排名
          </p>
        </div>
      </div>

      {/* 操作栏 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <ToolbarButton label="记录 1h" />
        <ToolbarButton label="记录 4h" />
        <ToolbarButton label="记录 1d" />
        <ToolbarButton label="记录 1w" />
        <ToolbarButton label="立即比对" primary />
        <ToolbarButton label="生成预测" />
        <ToolbarButton label="结算到期" />
      </div>

      {/* 自选币 */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">自选币：</span>
        {watchlist.map((c) => (
          <span
            key={c}
            className="inline-flex items-center gap-1 rounded-full bg-secondary px-2.5 py-0.5 text-xs"
          >
            {c}
            <button className="cursor-pointer text-muted-foreground transition-colors hover:text-chart-4" title="移除">
              ×
            </button>
          </span>
        ))}
        <input
          placeholder="添加如 LTCUSDT"
          className="h-7 w-32 rounded-md border border-input bg-transparent px-2 text-xs outline-none focus:border-primary"
        />
        <button className="h-7 rounded-md border border-input px-2 text-xs transition-colors hover:bg-accent">
          添加
        </button>
      </div>

      <div className="flex flex-col gap-5">
        {/* 实时快照 */}
        <SectionCard title="实时快照" hint="每个币 / 周期的最新一次记录">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-4 font-medium">币种</th>
                  {TFS.map((tf) => (
                    <th key={tf} className="py-2 pr-6 text-right font-medium">{tf}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {snapshots.map((row) => (
                  <tr key={row.coin} className="border-t border-border/60 align-top transition-colors hover:bg-muted/40">
                    <td className="py-3 pr-4 font-medium">{row.coin}</td>
                    {TFS.map((tf) => {
                      const m = row.metrics[tf];
                      return (
                        <td key={tf} className="py-3 pr-6">
                          <div className="flex flex-col items-end gap-0.5 text-right">
                            <div className="flex items-baseline gap-1.5">
                              <DirectionBadge dir={m.dir} />
                              <span className="tnum text-sm text-foreground">{fmtPrice(m.price)}</span>
                            </div>
                            <div className="text-[11px] text-muted-foreground tnum">
                              RSI {m.rsi.toFixed(1)} · ADX {m.adx.toFixed(1)}
                            </div>
                            <div className="text-[11px] text-muted-foreground tnum">
                              MACD {m.macd.toFixed(2)} · VR {m.vr.toFixed(2)}
                            </div>
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-[11px] text-muted-foreground">
            配色：<span className="text-chart-2 font-medium">多 / 强多</span> 看涨 ·
            <span className="text-chart-4 font-medium"> 空 / 强空</span> 看跌 · 平 中性
          </p>
        </SectionCard>

        {/* 最新比对 */}
        <SectionCard title="最新比对" hint="最近两次快照之差">
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            {TFS.map((tf) => (
              <CompareCard key={tf} tf={tf} />
            ))}
          </div>
        </SectionCard>

        {/* 检测分析 */}
        <SectionCard title="检测分析" hint="信号 / 背离 / 异常预警">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead>
                <tr className="text-muted-foreground">
                  <th className="py-1 pr-4 font-medium">币种</th>
                  {TFS.map((tf) => (
                    <th key={tf} className="py-1 pr-6 font-medium">{tf}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {snapshots.map((row) => (
                  <tr key={row.coin} className="border-t border-border/60">
                    <td className="py-2 pr-4 font-medium">{row.coin}</td>
                    {TFS.map((tf) => {
                      const sigs = analysis[row.coin]?.[tf];
                      return (
                        <td key={tf} className="py-2 pr-6">
                          <div className="flex flex-col gap-0.5">
                            {sigs && sigs.length > 0 ? (
                              sigs.map((s, i) => <SignalBadge key={i} s={s} />)
                            ) : (
                              <SignalBadge s={{ label: "持平", tone: "neutral" }} />
                            )}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            信号取最近两次快照之差（转多 / 转空、MACD 金叉 / 死叉）；背离为后半段价格创新高 / 低而 MACD 柱未同步（底背离看涨、顶背离看跌）；异常为放量 / 大涨大跌 / RSI 超买超卖。
          </p>
        </SectionCard>

        {/* 预测与胜率 */}
        <SectionCard title="预测与胜率" hint="预测方向 → 到点拉实时价核对 → 统计胜率">
          <div className="mb-4 flex flex-wrap items-end gap-6">
            <div>
              <p className="text-xs text-muted-foreground">整体胜率</p>
              <p className="text-3xl font-semibold tnum text-foreground">{winRate.overall}%</p>
              <p className="text-[11px] text-muted-foreground tnum">
                胜 {winRate.win} · 负 {winRate.loss} · 共 {winRate.total}
              </p>
            </div>
            <div className="flex gap-3">
              {winByTf.map((w) => (
                <div key={w.tf} className="rounded-md bg-secondary/60 px-3 py-2 text-center">
                  <p className="text-xs text-muted-foreground">{w.tf}</p>
                  <p className="text-lg font-semibold tnum">{w.rate}%</p>
                  <p className="text-[10px] text-muted-foreground tnum">{w.n} 笔</p>
                </div>
              ))}
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead>
                <tr className="text-muted-foreground">
                  <th className="py-1 pr-3 font-medium">币种</th>
                  <th className="py-1 pr-3 font-medium">周期</th>
                  <th className="py-1 pr-3 font-medium">预测</th>
                  <th className="py-1 pr-3 text-right font-medium">原价</th>
                  <th className="py-1 pr-3 text-right font-medium">预测价</th>
                  <th className="py-1 pr-3 text-right font-medium">实际价</th>
                  <th className="py-1 pr-3 font-medium">实际方向</th>
                  <th className="py-1 pr-3 font-medium">结果</th>
                  <th className="py-1 font-medium">预测时间</th>
                </tr>
              </thead>
              <tbody>
                {predictions.map((p) => (
                  <tr key={`${p.coin}-${p.tf}-${p.at}`} className="border-t border-border/60 transition-colors hover:bg-muted/40">
                    <td className="py-1.5 pr-3 font-medium">{p.coin}</td>
                    <td className="py-1.5 pr-3 tnum">{p.tf}</td>
                    <td className="py-1.5 pr-3">
                      <span className={p.bias === "看多" ? "text-chart-2" : "text-chart-4"}>{p.bias}</span>
                    </td>
                    <td className="py-1.5 pr-3 text-right tnum text-muted-foreground">{fmtPrice(p.origPrice)}</td>
                    <td className="py-1.5 pr-3 text-right tnum text-foreground">{fmtPrice(p.predPrice)}</td>
                    <td className="py-1.5 pr-3 text-right tnum">
                      {p.actualPrice === null ? "—" : fmtPrice(p.actualPrice)}
                    </td>
                    <td className="py-1.5 pr-3">
                      {p.actualDir ? <DirectionBadge dir={p.actualDir} /> : "—"}
                    </td>
                    <td className="py-1.5 pr-3"><ResultBadge result={p.result} /></td>
                    <td className="py-1.5 tnum text-muted-foreground">{p.at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </SectionCard>
      </div>

      <p className="mt-6 text-xs leading-relaxed text-muted-foreground">
        指标口径与全站一致（Wilder 标准 RSI / ATR / ADX、MACD(12,26,9)、布林(20,2)）。调度器随{" "}
        <code className="rounded bg-muted px-1">npm run dev</code> 启动：每小时记 1h、每 4h 记 4h、每 24h 记 1d、每 7 天记 1w，每日
        9:00 自动比对存档。预测环节：每个周期（1h / 4h / 1d / 1w）按“趋势 + MACD 同向”生成方向预测，到点拉实时价核对（主预测 1d 每天刷新），累计胜率。自选币可在此页增删（持久化到后端
        SQLite 的 watchlist 表）。数据存于 <code className="rounded bg-muted px-1">data/snapshots.db</code>。
      </p>
    </div>
  );
}
