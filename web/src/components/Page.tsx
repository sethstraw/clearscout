import type { PropsWithChildren, ReactNode } from "react";

export function Page({
  eyebrow,
  title,
  description,
  action,
  children,
  density = "default",
}: PropsWithChildren<{
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
  density?: "default" | "dense";
}>) {
  return (
    <main
      className={`page${density === "dense" ? " page--dense" : ""}`}
      id="main-content"
      tabIndex={-1}
    >
      <header className="page-header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h1>{title}</h1>
          <p className="page-description">{description}</p>
        </div>
        {action}
      </header>
      <div className="page-body">{children}</div>
    </main>
  );
}
