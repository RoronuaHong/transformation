import { isRangeVideoClip, type KeyPoint } from "@/lib/note-points";
import { NoteFrame } from "@/components/NoteFrame";

export function NoteList({
  heading,
  items,
  viewFrameLabel = "点击查看",
  closeLabel = "关闭",
}: {
  heading: string;
  items: KeyPoint[];
  viewFrameLabel?: string;
  closeLabel?: string;
  /** @deprecated range MP4s are only listed under 「下载片段」 */
  clipLabel?: string;
}) {
  // Study notes: hide range-only rows (those belong under 「下载片段」).
  const visible = items.filter((kp) => !isRangeVideoClip(kp));
  if (!visible.length) return null;
  return (
    <div className="notes-block">
      {heading ? <h3 className="section-title">{heading}</h3> : null}
      <ol className="notes-list">
        {visible.map((kp) => (
          <li key={kp.title} className={kp.image ? "has-frame" : undefined}>
            <strong>{kp.title}</strong>
            {kp.detail ? <span>{kp.detail}</span> : null}
            {kp.image ? (
              <NoteFrame src={kp.image} viewLabel={viewFrameLabel} closeLabel={closeLabel} />
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  );
}
