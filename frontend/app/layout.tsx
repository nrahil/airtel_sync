import type { Metadata, Viewport } from "next";
import "./globals.css";
import { ServiceWorkerRegistrar } from "@/components/ServiceWorkerRegistrar";

export const metadata: Metadata = {
  title: "Udyam Digital",
  description: "Find, buy and activate the right business bundle — on your phone, in your language.",
  applicationName: "Udyam Digital",
  appleWebApp: { capable: true, title: "Udyam Digital", statusBarStyle: "default" },
  icons: { icon: "/icon.svg" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#b91c1c",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    // Each localized page sets `lang` on its <main>; the shell itself is English.
    <html lang="en">
      <body className="antialiased">
        {children}
        <ServiceWorkerRegistrar />
      </body>
    </html>
  );
}
