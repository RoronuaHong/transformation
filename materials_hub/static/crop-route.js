/* 素材中心 · 裁剪/工作台:同域路由 /zh(经 hub gateway 反代 transform),不再 iframe。 */
const CROP_DEFAULT = "/zh";

function cropPath() {
  return localStorage.getItem("hub_crop_path") || CROP_DEFAULT;
}

function goCrop() {
  location.assign(cropPath());
}

function bindCropUI() {
  const navBtn = document.getElementById("cropNav");
  if (navBtn) navBtn.onclick = () => goCrop();
  // 旧书签 #/crop → 同域工作台
  if (location.hash.startsWith("#/crop")) {
    location.replace(cropPath());
  }
}

bindCropUI();
