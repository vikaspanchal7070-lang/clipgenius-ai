import "./globals.css";
export const metadata = {
  title: "ClipGenius AI — Turn long videos into shorts",
  description: "AI-powered video repurposing studio."
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body>{children}</body></html>;
}
