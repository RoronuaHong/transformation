/** Workbench package presets (跟课包 / 二创包 / 高级). */

export type TryPackageId = "course" | "remix" | "advanced";

export type TryPackageToggles = {
  wantTranslate: boolean;
  wantNotes: boolean;
  wantClips: boolean;
  wantDehardsub: boolean;
  wantDeblur: boolean;
  wantEnhance: boolean;
  wantCompress: boolean;
  wantConcat: boolean;
  wantRemix: boolean;
  wantPublish: boolean;
};

/** 跟课包：译 + 笔记。二创包：去硬字幕 + 笔记对齐切片 + 二创 + 可选压缩。 */
export const TRY_PACKAGE_TOGGLES: Record<
  Exclude<TryPackageId, "advanced">,
  TryPackageToggles
> = {
  course: {
    wantTranslate: true,
    wantNotes: true,
    wantClips: false,
    wantDehardsub: false,
    wantDeblur: false,
    wantEnhance: false,
    wantCompress: false,
    wantConcat: false,
    wantRemix: false,
    wantPublish: false,
  },
  remix: {
    wantTranslate: false,
    wantNotes: true,
    wantClips: true,
    wantDehardsub: true,
    wantDeblur: false,
    wantEnhance: false,
    wantCompress: true,
    wantConcat: false,
    wantRemix: true,
    wantPublish: false,
  },
};

export function applyTryPackage(
  id: TryPackageId,
  current: TryPackageToggles
): TryPackageToggles {
  if (id === "advanced") return current;
  return { ...TRY_PACKAGE_TOGGLES[id] };
}
