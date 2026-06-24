import Sidebar from "./_components/Sidebar";

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="md:flex md:gap-8 scroll-smooth">
      <Sidebar />
      <article className="min-w-0 flex-1 max-w-3xl">{children}</article>
    </div>
  );
}
