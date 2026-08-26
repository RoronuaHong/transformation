"use client";

import type { TryCopy } from "@/lib/copy";

export type ClipRow = { start: string; end: string };

export type SourceMediaState = {
  open: boolean;
  frames: "auto" | "none" | "gif" | "jpg";
  gifRanges: ClipRow[];
  clips: ClipRow[];
};

export function defaultSourceMedia(
  frames: SourceMediaState["frames"] = "auto"
): SourceMediaState {
  return {
    open: true,
    frames,
    gifRanges: [{ start: "", end: "" }],
    clips: [{ start: "", end: "" }],
  };
}

export function cloneSourceMedia(
  frames: SourceMediaState["frames"],
  gifRanges: ClipRow[],
  clips: ClipRow[]
): SourceMediaState {
  return {
    open: true,
    frames,
    gifRanges: gifRanges.map((r) => ({ ...r })),
    clips: clips.map((r) => ({ ...r })),
  };
}

type Props = {
  sourceKey: string;
  label: string;
  copy: TryCopy;
  media: SourceMediaState;
  clipMax: number;
  loading: boolean;
  gifError: string | null;
  clipError: string | null;
  onToggle: () => void;
  onFrames: (frames: SourceMediaState["frames"]) => void;
  onRange: (
    which: "gif" | "mp4",
    index: number,
    key: "start" | "end",
    raw: string
  ) => void;
  onAddRange: (which: "gif" | "mp4") => void;
  onRemoveRange: (which: "gif" | "mp4", index: number) => void;
};

export function TrySourceMediaCard({
  sourceKey,
  label,
  copy,
  media: lm,
  clipMax,
  loading,
  gifError,
  clipError,
  onToggle,
  onFrames,
  onRange,
  onAddRange,
  onRemoveRange,
}: Props) {
  const short = label.length > 72 ? `${label.slice(0, 69)}…` : label;

  return (
    <div className="try-link-card" data-source={sourceKey}>
      <div className="try-link-card-head">
        <p className="try-link-card-url" title={label}>
          {short}
        </p>
        <button
          type="button"
          className="try-link-card-toggle"
          onClick={onToggle}
          disabled={loading}
        >
          {lm.open ? copy.linkMediaCollapse : copy.linkMediaExpand}
        </button>
      </div>
      {lm.open ? (
        <div className="try-link-card-body">
          <div className="try-frames-options" role="radiogroup">
            {(
              [
                ["auto", copy.framesAuto],
                ["none", copy.framesNone],
                ["gif", copy.framesGif],
                ["jpg", copy.framesJpg],
              ] as const
            ).map(([value, frameLabel]) => (
              <label
                key={`${sourceKey}-${value}`}
                className={lm.frames === value ? "is-on" : undefined}
              >
                <input
                  type="radio"
                  name={`frames-${sourceKey}`}
                  value={value}
                  checked={lm.frames === value}
                  disabled={loading}
                  onChange={() => onFrames(value)}
                />
                <span>{frameLabel}</span>
              </label>
            ))}
          </div>
          {lm.frames !== "none" ? (
            <fieldset className="try-media-block try-frames compact" disabled={loading}>
              <legend className="try-media-title">{copy.framesBlockTitle}</legend>
              <div className="try-media-body">
                {(lm.gifRanges.length ? lm.gifRanges : [{ start: "", end: "" }]).map(
                  (row, i) => (
                    <div className="try-clip-row" key={`${sourceKey}-gif-${i}`}>
                      <label className="try-field">
                        <span>{copy.clipsStart}</span>
                        <input
                          type="number"
                          min={0}
                          max={clipMax}
                          step={0.1}
                          value={row.start}
                          disabled={loading}
                          onChange={(e) => onRange("gif", i, "start", e.target.value)}
                          aria-invalid={Boolean(gifError)}
                        />
                      </label>
                      <span className="try-clip-arrow" aria-hidden="true">
                        →
                      </span>
                      <label className="try-field">
                        <span>{copy.clipsEnd}</span>
                        <input
                          type="number"
                          min={0}
                          max={clipMax}
                          step={0.1}
                          value={row.end}
                          disabled={loading}
                          onChange={(e) => onRange("gif", i, "end", e.target.value)}
                          aria-invalid={Boolean(gifError)}
                        />
                      </label>
                      <button
                        type="button"
                        className="try-clip-remove"
                        onClick={() => onRemoveRange("gif", i)}
                      >
                        {copy.clipsRemove}
                      </button>
                    </div>
                  )
                )}
                <button
                  type="button"
                  className="try-clip-add"
                  disabled={lm.gifRanges.length >= 24}
                  onClick={() => onAddRange("gif")}
                >
                  {copy.gifAdd}
                </button>
              </div>
              {gifError ? <p className="try-error">{gifError}</p> : null}
            </fieldset>
          ) : null}
          <fieldset className="try-media-block try-clips compact" disabled={loading}>
            <legend className="try-media-title">{copy.clipsBlockTitle}</legend>
            <div className="try-media-body">
              {(lm.clips.length ? lm.clips : [{ start: "", end: "" }]).map((row, i) => (
                <div className="try-clip-row" key={`${sourceKey}-mp4-${i}`}>
                  <label className="try-field">
                    <span>{copy.clipsStart}</span>
                    <input
                      type="number"
                      min={0}
                      max={clipMax}
                      step={0.1}
                      value={row.start}
                      disabled={loading}
                      onChange={(e) => onRange("mp4", i, "start", e.target.value)}
                      aria-invalid={Boolean(clipError)}
                    />
                  </label>
                  <span className="try-clip-arrow" aria-hidden="true">
                    →
                  </span>
                  <label className="try-field">
                    <span>{copy.clipsEnd}</span>
                    <input
                      type="number"
                      min={0}
                      max={clipMax}
                      step={0.1}
                      value={row.end}
                      disabled={loading}
                      onChange={(e) => onRange("mp4", i, "end", e.target.value)}
                      aria-invalid={Boolean(clipError)}
                    />
                  </label>
                  <button
                    type="button"
                    className="try-clip-remove"
                    onClick={() => onRemoveRange("mp4", i)}
                  >
                    {copy.clipsRemove}
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="try-clip-add"
                disabled={lm.clips.length >= 24}
                onClick={() => onAddRange("mp4")}
              >
                {copy.clipsAdd}
              </button>
            </div>
            {clipError ? <p className="try-error">{clipError}</p> : null}
          </fieldset>
        </div>
      ) : null}
    </div>
  );
}
