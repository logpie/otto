"use client";

import Link from "next/link";
import { useSession, signOut } from "next-auth/react";
import { useState } from "react";
import { useRouter } from "next/navigation";

export default function Navbar() {
  const { data: session } = useSession();
  const router = useRouter();
  const [search, setSearch] = useState("");

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (search.trim()) {
      router.push(`/search?q=${encodeURIComponent(search.trim())}`);
      setSearch("");
    }
  };

  return (
    <nav className="bg-white shadow-sm border-b">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between items-center h-16">
          <div className="flex items-center gap-6">
            <Link href="/" className="text-xl font-bold text-orange-600">
              RecipeShare
            </Link>
            <Link href="/" className="text-gray-600 hover:text-gray-900">
              Browse
            </Link>
            {session && (
              <>
                <Link
                  href="/recipes/new"
                  className="text-gray-600 hover:text-gray-900"
                >
                  New Recipe
                </Link>
                <Link
                  href="/favorites"
                  className="text-gray-600 hover:text-gray-900"
                >
                  Favorites
                </Link>
              </>
            )}
          </div>

          <form onSubmit={handleSearch} className="flex-1 max-w-md mx-4">
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search recipes..."
              className="w-full px-4 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
            />
          </form>

          <div className="flex items-center gap-4">
            {session ? (
              <>
                <span className="text-sm text-gray-600">
                  {session.user?.name}
                </span>
                <button
                  onClick={() => signOut()}
                  className="text-sm text-gray-600 hover:text-gray-900"
                >
                  Sign Out
                </button>
              </>
            ) : (
              <>
                <Link
                  href="/login"
                  className="text-sm text-gray-600 hover:text-gray-900"
                >
                  Login
                </Link>
                <Link
                  href="/register"
                  className="text-sm bg-orange-600 text-white px-4 py-2 rounded-lg hover:bg-orange-700"
                >
                  Register
                </Link>
              </>
            )}
          </div>
        </div>
      </div>
    </nav>
  );
}
