/* 素材中心 · 裁剪工具路由(#/crop):iframe 嵌入 transform 裁剪工作台。
   依赖主面板的 $ / t / load;由 index.html 在 hashchange/init 时调用 route()。 */
const CROP_DEFAULT = "http://127.0.0.1:3000/zh";

function cropUrl() {
  return localStorage.getItem("hub_crop_url") || CROP_DEFAULT;
}

function showCrop(on) {
  const cv = document.getElementById("cropView");
  const lay = document.querySelector(".layout");
  if (!cv || !lay) return;
  cv.style.display = on ? "block" : "none";
  lay.style.display = on ? "none" : "flex";
  const frame = document.getElementById("cropFrame");
  if (on) {
    document.getElementById("cropAddr").value = cropUrl();
    frame.src = cropUrl();
  } else if (frame) {
    frame.src = "about:blank";
  }
}

function route() {
  showCrop(location.hash.startsWith("#/crop"));
}

function bindCropUI() {
  const loadBtn = document.getElementById("cropLoadBtn");
  const openBtn = document.getElementById("cropOpenBtn");
  const navBtn = document.getElementById("cropNav");
  const backBtn = document.getElementById("cropBackBtn");
  if (loadBtn) loadBtn.onclick = () => {
    const u = document.getElementById("cropAddr").value.trim();
    if (u) { localStorage.setItem("hub_crop_url", u); document.getElementById("cropFrame").src = u; }
  };
  if (openBtn) openBtn.onclick = () =>
    window.open(document.getElementById("cropAddr").value.trim() || cropUrl());
  if (navBtn) navBtn.onclick = () => { location.hash = "#/crop"; };
  if (backBtn) backBtn.onclick = () => { location.hash = "#size=48"; route(); };
}

/* 本脚本在主脚本之后加载:补一个自己的 hashchange 监听 + 初始路由 + 绑定按钮 */
window.addEventListener("hashchange", () => route());
bindCropUI();
route();
