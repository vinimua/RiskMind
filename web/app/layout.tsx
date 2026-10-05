import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "信贷风控模型监测平台",
  description: "独立开发骨架",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
