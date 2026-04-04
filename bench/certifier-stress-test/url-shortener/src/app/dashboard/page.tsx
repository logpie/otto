"use client";

import { useSession, signOut } from "next-auth/react";
import { useRouter } from "next/navigation";
import { useEffect, useState, useCallback } from "react";

interface Link {
  id: string;
  code: string;
  url: string;
  clicks: number;
  lastClickedAt: string | null;
  createdAt: string;
}

export default function DashboardPage() {
  const { data: session, status } = useSession();
  const router = useRouter();
  const [links, setLinks] = useState<Link[]>([]);
  const [url, setUrl] = useState("");
  const [customCode, setCustomCode] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [qrLinkId, setQrLinkId] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const fetchLinks = useCallback(async () => {
    const res = await fetch("/api/links");
    if (res.ok) setLinks(await res.json());
  }, []);

  useEffect(() => {
    if (status === "unauthenticated") router.push("/login");
    if (status === "authenticated") fetchLinks();
  }, [status, router, fetchLinks]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setCreating(true);

    const res = await fetch("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, code: customCode || undefined }),
    });

    setCreating(false);

    if (!res.ok) {
      const data = await res.json();
      setError(data.error);
    } else {
      setUrl("");
      setCustomCode("");
      fetchLinks();
    }
  }

  async function handleDelete(id: string) {
    await fetch(`/api/links/${id}`, { method: "DELETE" });
    fetchLinks();
  }

  function copyToClipboard(code: string) {
    const shortUrl = `${window.location.origin}/${code}`;
    navigator.clipboard.writeText(shortUrl);
    setCopied(code);
    setTimeout(() => setCopied(null), 2000);
  }

  if (status === "loading") {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-zinc-500">Loading...</p>
      </div>
    );
  }

  if (!session) return null;

  const baseUrl = typeof window !== "undefined" ? window.location.origin : "";

  return (
    <div className="flex-1 max-w-4xl mx-auto w-full px-4 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="text-zinc-500 dark:text-zinc-400 text-sm">
            Welcome, {session.user.name}
          </p>
        </div>
        <button
          onClick={() => signOut({ callbackUrl: "/" })}
          className="px-4 py-2 text-sm rounded-lg border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-50 dark:hover:bg-zinc-800 transition-colors"
        >
          Sign Out
        </button>
      </div>

      {/* Create form */}
      <form onSubmit={handleCreate} className="mb-8 p-6 rounded-xl border border-zinc-200 dark:border-zinc-800">
        <h2 className="text-lg font-semibold mb-4">Shorten a URL</h2>
        {error && (
          <div className="mb-4 p-3 text-sm text-red-600 bg-red-50 dark:bg-red-900/20 dark:text-red-400 rounded-lg">
            {error}
          </div>
        )}
        <div className="flex flex-col sm:flex-row gap-3">
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/very-long-url"
            required
            className="flex-1 px-3 py-2 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent focus:outline-none focus:ring-2 focus:ring-zinc-500"
          />
          <input
            type="text"
            value={customCode}
            onChange={(e) => setCustomCode(e.target.value)}
            placeholder="Custom code (optional)"
            pattern="[a-zA-Z0-9_-]+"
            className="sm:w-48 px-3 py-2 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-transparent focus:outline-none focus:ring-2 focus:ring-zinc-500"
          />
          <button
            type="submit"
            disabled={creating}
            className="px-6 py-2 rounded-lg bg-zinc-900 text-white font-medium hover:bg-zinc-800 dark:bg-white dark:text-zinc-900 dark:hover:bg-zinc-200 transition-colors disabled:opacity-50 whitespace-nowrap"
          >
            {creating ? "Creating..." : "Shorten"}
          </button>
        </div>
      </form>

      {/* Links table */}
      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Your Links ({links.length})</h2>

        {links.length === 0 ? (
          <p className="text-zinc-500 dark:text-zinc-400 py-8 text-center">
            No links yet. Create your first short URL above.
          </p>
        ) : (
          <div className="border border-zinc-200 dark:border-zinc-800 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-900">
                <tr>
                  <th className="px-4 py-3 text-left font-medium">Short URL</th>
                  <th className="px-4 py-3 text-left font-medium hidden sm:table-cell">Destination</th>
                  <th className="px-4 py-3 text-center font-medium">Clicks</th>
                  <th className="px-4 py-3 text-left font-medium hidden md:table-cell">Last Clicked</th>
                  <th className="px-4 py-3 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {links.map((link) => (
                  <tr key={link.id} className="hover:bg-zinc-50 dark:hover:bg-zinc-900/50">
                    <td className="px-4 py-3">
                      <button
                        onClick={() => copyToClipboard(link.code)}
                        className="text-blue-600 dark:text-blue-400 hover:underline font-mono text-xs"
                        title="Click to copy"
                      >
                        {baseUrl}/{link.code}
                      </button>
                      {copied === link.code && (
                        <span className="ml-2 text-xs text-green-600 dark:text-green-400">Copied!</span>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden sm:table-cell">
                      <span className="truncate block max-w-[200px] text-zinc-500 dark:text-zinc-400" title={link.url}>
                        {link.url}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center font-mono">{link.clicks}</td>
                    <td className="px-4 py-3 hidden md:table-cell text-zinc-500 dark:text-zinc-400">
                      {link.lastClickedAt
                        ? new Date(link.lastClickedAt).toLocaleDateString()
                        : "Never"}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => setQrLinkId(qrLinkId === link.id ? null : link.id)}
                          className="p-1.5 rounded hover:bg-zinc-200 dark:hover:bg-zinc-700 transition-colors"
                          title="QR Code"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <rect width="5" height="5" x="3" y="3" rx="1" />
                            <rect width="5" height="5" x="16" y="3" rx="1" />
                            <rect width="5" height="5" x="3" y="16" rx="1" />
                            <path d="M21 16h-3a2 2 0 0 0-2 2v3" />
                            <path d="M21 21v.01" />
                            <path d="M12 7v3a2 2 0 0 1-2 2H7" />
                            <path d="M3 12h.01" />
                            <path d="M12 3h.01" />
                            <path d="M12 16v.01" />
                            <path d="M16 12h1" />
                            <path d="M21 12v.01" />
                            <path d="M12 21v-1" />
                          </svg>
                        </button>
                        <button
                          onClick={() => handleDelete(link.id)}
                          className="p-1.5 rounded hover:bg-red-100 dark:hover:bg-red-900/30 text-red-600 dark:text-red-400 transition-colors"
                          title="Delete"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M3 6h18" />
                            <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" />
                            <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                          </svg>
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {/* QR Code display */}
            {qrLinkId && (
              <div className="p-6 border-t border-zinc-200 dark:border-zinc-800 flex flex-col items-center gap-3">
                <p className="text-sm font-medium">QR Code</p>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={`/api/links/${qrLinkId}/qr`}
                  alt="QR Code"
                  className="w-48 h-48"
                />
                <a
                  href={`/api/links/${qrLinkId}/qr`}
                  download="qrcode.png"
                  className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                >
                  Download QR Code
                </a>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
