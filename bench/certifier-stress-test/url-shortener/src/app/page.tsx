import Link from "next/link";

export default function Home() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-4">
      <div className="text-center max-w-2xl">
        <h1 className="text-6xl font-bold tracking-tight mb-4">Snip</h1>
        <p className="text-xl text-zinc-500 dark:text-zinc-400 mb-8">
          Shorten your links, track your clicks, share with ease.
        </p>
        <div className="flex gap-4 justify-center">
          <Link
            href="/login"
            className="px-6 py-3 rounded-lg bg-zinc-900 text-white font-medium hover:bg-zinc-800 dark:bg-white dark:text-zinc-900 dark:hover:bg-zinc-200 transition-colors"
          >
            Sign In
          </Link>
          <Link
            href="/register"
            className="px-6 py-3 rounded-lg border border-zinc-300 dark:border-zinc-700 font-medium hover:bg-zinc-50 dark:hover:bg-zinc-800 transition-colors"
          >
            Create Account
          </Link>
        </div>
      </div>
    </div>
  );
}
