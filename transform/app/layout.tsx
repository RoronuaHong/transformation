import type { Metadata, Viewport } from "next";
import Script from "next/script";
import "./globals.css";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000"),
  title: {
    default: "Vitual",
    template: "%s · Vitual",
  },
  description: "Turn spoken video into multilingual notes, captions, and step clips.",
};

/** Apply saved / system theme before paint to avoid flash. */
const THEME_BOOT = `(function(){
  try{
    var k="vitual-theme";
    var t=localStorage.getItem(k);
    if(t!=="light"&&t!=="dark"){
      t=window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
    }
    document.documentElement.setAttribute("data-theme",t);
    document.documentElement.style.colorScheme=t;
  }catch(e){}
})();`;

/** Runs before hydration; mouse/gesture extensions inject empty #crx-* nodes into body. */
const SCRUB_EXTENSIONS = `(function(){
  function safe(n){
    if(!n||n.nodeType!==1)return;
    var id=n.id||"";
    if(id!=="crx-mouse-redesign-content-root"&&id.indexOf("crx-")!==0)return;
    if(n.querySelector&&n.querySelector(".shell,.nav,main,.try-panel")){
      n.removeAttribute("id");
      n.removeAttribute("style");
      return;
    }
    n.remove();
  }
  function sweep(){
    document.querySelectorAll("#crx-mouse-redesign-content-root,[id^=\\"crx-\\"]").forEach(safe);
  }
  sweep();
  try{
    new MutationObserver(function(ms){
      for(var i=0;i<ms.length;i++){
        var nodes=ms[i].addedNodes;
        for(var j=0;j<nodes.length;j++) safe(nodes[j]);
      }
    }).observe(document.documentElement,{childList:true,subtree:true});
  }catch(e){}
})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh" suppressHydrationWarning data-theme="light">
      <body suppressHydrationWarning>
        <Script id="theme-boot" strategy="beforeInteractive">
          {THEME_BOOT}
        </Script>
        <Script id="scrub-crx" strategy="beforeInteractive">
          {SCRUB_EXTENSIONS}
        </Script>
        {/* Bait: some extensions attach to the first body div instead of the app shell. */}
        <div hidden aria-hidden="true" data-ext-bait="" suppressHydrationWarning />
        {children}
      </body>
    </html>
  );
}
