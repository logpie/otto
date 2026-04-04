"use client";

import Link from "next/link";
import { useSession, signOut } from "next-auth/react";

export default function Navbar() {
  const { data: session } = useSession();

  return (
    <nav className="bg-white border-b border-gray-200">
      <div className="max-w-4xl mx-auto px-4 py-3 flex items-center justify-between">
        <Link href="/" className="text-xl font-bold text-gray-900">
          Blog
        </Link>
        <div className="flex items-center gap-4">
          <Link href="/posts" className="text-gray-600 hover:text-gray-900">
            Posts
          </Link>
          {session ? (
            <>
              <Link
                href="/posts/new"
                className="text-gray-600 hover:text-gray-900"
              >
                Write
              </Link>
              <Link
                href={`/profile/${session.user.id}`}
                className="text-gray-600 hover:text-gray-900"
              >
                Profile
              </Link>
              <button
                onClick={() => signOut()}
                className="text-gray-600 hover:text-gray-900"
              >
                Logout
              </button>
            </>
          ) : (
            <>
              <Link
                href="/login"
                className="text-gray-600 hover:text-gray-900"
              >
                Login
              </Link>
              <Link
                href="/register"
                className="bg-blue-600 text-white px-3 py-1.5 rounded-md text-sm hover:bg-blue-700"
              >
                Register
              </Link>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}
