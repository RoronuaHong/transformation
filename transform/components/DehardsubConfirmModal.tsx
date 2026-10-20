"use client";

import { useRef, useState } from "react";

export type DhBox = { x: number; y: number; w: number; h: number; role: string };

export type DhDynamic = {
  role: string;
  fps: number;
  width: number;
  height: number;
  motion_px?: number;
  representative: { x: number; y: number; w: number; h: number };
  samples: Array<{ frame: number; x: number; y: number; w: number; h: number }>;
  trajectory: Array<{ frame: number; x: number; y: number; w: number; h: number }>;
};

const ROLE_ORDER = ["dialogue", "title", "watermark", "up_watermark"] as const;

const ROLE_COLOR: Record<string, string> = {
  dialogue: "#22d3ee",
  title: "#facc15",
  watermark: "#fb923c",
  up_watermark: "#e879f9",
};

const ROLE_LABEL: Record<string, string> = {
  dialogue: "字幕",
  title: "标题",
  watermark: "水印",
  up_watermark: "UP水印",
};

type Props = {
  previewSrc: string;
  width: number;
  height: number;
  boxes: DhBox[];
  dynamicWatermarks?: DhDynamic[];
  loading?: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: (boxes: DhBox[], dynamic: DhDynamic[]) => void;
};

type Drag =
  | { mode: "move" | "resize"; i: number; sx: number; sy: number; box: DhBox }
  | null;

/**
 * Human-in-the-loop confirm for dehardsub: shows the auto-detected boxes on the
 * preview frame and lets the user move / resize / add / delete boxes and cycle
 * their role before processing starts.
 */
export function DehardsubConfirmModal(props: Props) {
  const { previewSrc, width, height, loading, error, onCancel, onConfirm } = props;
  const dynWms = props.dynamicWatermarks || [];
  const [boxes, setBoxes] = useState<DhBox[]>(props.boxes);
  const [sel, setSel] = useState<number | null>(props.boxes.length ? 0 : null);
  // Opt-in moving watermarks: none selected by default (detector may over-report
  // on busy content), so a false positive is never removed unless explicitly ticked.
  const [selDyn, setSelDyn] = useState<number[]>([]);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<Drag>(null);

  const pct = (v: number, total: number) => `${(v / total) * 100}%`;

  function onDown(e: React.PointerEvent, i: number, mode: "move" | "resize") {
    e.preventDefault();
    e.stopPropagation();
    try {
      (e.currentTarget as Element).setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dragRef.current = { mode, i, sx: e.clientX, sy: e.clientY, box: { ...boxes[i] } };
    setSel(i);
  }

  function onMove(e: React.PointerEvent) {
    const d = dragRef.current;
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!d || !rect) return;
    const dx = ((e.clientX - d.sx) / rect.width) * width;
    const dy = ((e.clientY - d.sy) / rect.height) * height;
    setBoxes((prev) =>
      prev.map((b, i) => {
        if (i !== d.i) return b;
        if (d.mode === "move") {
          const nx = Math.max(0, Math.min(width - b.w, d.box.x + dx));
          const ny = Math.max(0, Math.min(height - b.h, d.box.y + dy));
          return { ...b, x: Math.round(nx), y: Math.round(ny) };
        }
        const nw = Math.max(20, Math.min(width - b.x, d.box.w + dx));
        const nh = Math.max(20, Math.min(height - b.y, d.box.h + dy));
        return { ...b, w: Math.round(nw), h: Math.round(nh) };
      })
    );
  }

  function onUp() {
    dragRef.current = null;
  }

  function addBox() {
    const w = Math.round(width * 0.6);
    const h = Math.round(Math.max(40, height * 0.06));
    const nb: DhBox = {
      x: Math.round((width - w) / 2),
      y: Math.round(height * 0.82),
      w,
      h,
      role: "dialogue",
    };
    setBoxes((b) => [...b, nb]);
    setSel(boxes.length);
  }

  function removeBox(i: number) {
    setBoxes((b) => b.filter((_, k) => k !== i));
    setSel(null);
  }

  function cycleRole(i: number) {
    setBoxes((b) =>
      b.map((bx, k) =>
        k === i
          ? {
              ...bx,
              role: ROLE_ORDER[(ROLE_ORDER.indexOf(bx.role as (typeof ROLE_ORDER)[number]) + 1) % ROLE_ORDER.length],
            }
          : bx
      )
    );
  }

  return (
    <div className="dh-modal-backdrop" role="dialog" aria-modal="true">
      <div className="dh-modal">
        <header className="dh-modal-head">
          <h3>确认去字幕 / 去水印区域</h3>
          <p>
            自动检测到 {props.boxes.length} 个区域。拖拽移动、右下角缩放、可增删；点角色名可切换
            （字幕 / 标题 / 水印 / UP水印）。确认后才开始处理。
          </p>
        </header>

        <div className="dh-modal-body">
          <div
            className="dh-img-wrap"
            ref={wrapRef}
            style={{ aspectRatio: `${width} / ${height}` }}
          >
            <img src={previewSrc} alt="检测预览" draggable={false} />
            {boxes.map((b, i) => (
              <div
                key={i}
                className={`dh-box${sel === i ? " sel" : ""}`}
                style={{
                  left: pct(b.x, width),
                  top: pct(b.y, height),
                  width: pct(b.w, width),
                  height: pct(b.h, height),
                  borderColor: ROLE_COLOR[b.role] || "#22d3ee",
                }}
                onPointerDown={(e) => onDown(e, i, "move")}
                onPointerMove={onMove}
                onPointerUp={onUp}
                onClick={() => setSel(i)}
              >
                <span
                  className="dh-box-tag"
                  style={{ background: ROLE_COLOR[b.role] || "#22d3ee" }}
                >
                  {ROLE_LABEL[b.role] || b.role}
                </span>
                <span
                  className="dh-box-handle"
                  style={{ borderColor: ROLE_COLOR[b.role] || "#22d3ee" }}
                  onPointerDown={(e) => onDown(e, i, "resize")}
                  onPointerMove={onMove}
                  onPointerUp={onUp}
                />
              </div>
            ))}
          </div>

          <ul className="dh-box-list">
            {boxes.map((b, i) => (
              <li key={i} className={sel === i ? "sel" : ""}>
                <button
                  type="button"
                  className="dh-role"
                  style={{ borderColor: ROLE_COLOR[b.role] || "#22d3ee" }}
                  onClick={() => cycleRole(i)}
                  title="点击切换角色"
                >
                  {ROLE_LABEL[b.role] || b.role}
                </button>
                <span className="dh-coords">
                  {b.x},{b.y} · {b.w}×{b.h}
                </span>
                <button
                  type="button"
                  className="dh-del"
                  onClick={() => removeBox(i)}
                  aria-label="删除"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>

          {dynWms.length ? (
            <div className="dh-dynamic">
              <p className="dh-dynamic-title">
                检测到 {dynWms.length} 个<b>会移动</b>的水印（建议，默认不处理）。勾选后按逐帧轨迹修补；
                检测在繁忙画面可能误报，请确认后再勾选。
              </p>
              {dynWms.map((dwd, i) => (
                <label
                  key={i}
                  className={`dh-dyn-item${selDyn.includes(i) ? " sel" : ""}`}
                >
                  <input
                    type="checkbox"
                    checked={selDyn.includes(i)}
                    onChange={() =>
                      setSelDyn((s) =>
                        s.includes(i) ? s.filter((k) => k !== i) : [...s, i]
                      )
                    }
                    disabled={loading}
                  />
                  <span className="dh-dyn-info">
                    动态水印 · 位移 {Math.round(dwd.motion_px || 0)}px ·{" "}
                    {dwd.trajectory.length} 帧轨迹
                    <br />
                    <small>
                      起点 {dwd.representative.x},{dwd.representative.y}（
                      {dwd.representative.w}×{dwd.representative.h}）
                    </small>
                  </span>
                </label>
              ))}
            </div>
          ) : null}
        </div>

        {error ? <p className="dh-error">{error}</p> : null}

        <footer className="dh-modal-foot">
          <button type="button" className="dh-add" onClick={addBox} disabled={loading}>
            + 新增框
          </button>
          <span className="dh-spacer" />
          <button type="button" className="dh-cancel" onClick={onCancel} disabled={loading}>
            取消
          </button>
          <button
            type="button"
            className="dh-ok"
            onClick={() => onConfirm(boxes, selDyn.map((i) => dynWms[i]))}
            disabled={loading || (boxes.length === 0 && selDyn.length === 0)}
          >
            {loading ? "处理中…" : `确认开始（${boxes.length} 静态 + ${selDyn.length} 动态）`}
          </button>
        </footer>
      </div>
    </div>
  );
}
