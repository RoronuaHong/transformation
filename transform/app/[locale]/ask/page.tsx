import { isLocale, type Locale } from "@/lib/locales";
import { redirect } from "next/navigation";
import AskPanel from "@/components/AskPanel";

export default async function AskPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) redirect("/zh");
  const locale = raw as Locale;
  return (
    <main style={{ padding: "32px 16px", minHeight: "100vh", background: "#0b0f17", color: "#fff" }}>
      <h1 style={{ textAlign: "center", fontSize: 22, marginBottom: 4 }}>问问一桌</h1>
      <p style={{ textAlign: "center", opacity: 0.6, fontSize: 13, marginBottom: 16 }}>
        基于项目知识库（Milvus + 本地大模型）的智能问答
      </p>
      <AskPanel />
    </main>
  );
}
