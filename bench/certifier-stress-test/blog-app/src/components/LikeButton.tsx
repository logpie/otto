"use client";

import { useState } from "react";
import { useSession } from "next-auth/react";

export default function LikeButton({
  postId,
  initialLiked,
  initialCount,
}: {
  postId: string;
  initialLiked: boolean;
  initialCount: number;
}) {
  const { data: session } = useSession();
  const [liked, setLiked] = useState(initialLiked);
  const [count, setCount] = useState(initialCount);

  async function handleLike() {
    if (!session) return;

    const res = await fetch(`/api/posts/${postId}/like`, { method: "POST" });
    const data = await res.json();
    setLiked(data.liked);
    setCount((prev) => (data.liked ? prev + 1 : prev - 1));
  }

  return (
    <button
      onClick={handleLike}
      disabled={!session}
      className={`flex items-center gap-2 px-4 py-2 rounded-md text-sm ${
        liked
          ? "bg-red-50 text-red-600 border border-red-200"
          : "bg-gray-50 text-gray-600 border border-gray-200"
      } ${!session ? "opacity-50 cursor-not-allowed" : "hover:bg-gray-100"}`}
    >
      {liked ? "\u2665" : "\u2661"} {count} {count === 1 ? "like" : "likes"}
    </button>
  );
}
