import Link from "next/link";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-4 px-6">
      <h1 className="text-3xl font-semibold tracking-tight">Udyam Digital</h1>
      <p className="text-base text-neutral-600 dark:text-neutral-300">
        Find, buy and activate the right business bundle — on your phone, in your language.
      </p>
      <p className="text-sm text-neutral-500" data-testid="shell-status">
        App shell ready.
      </p>
      <div className="flex flex-wrap gap-3">
        <Link href="/hi/advisor" lang="hi-IN" className="rounded-xl bg-red-700 px-4 py-3 font-medium text-white">
          अपना प्लान चुनें
        </Link>
        <Link href="/en/advisor" className="rounded-xl border border-neutral-300 px-4 py-3 font-medium">
          Find your plan
        </Link>
      </div>
    </main>
  );
}
