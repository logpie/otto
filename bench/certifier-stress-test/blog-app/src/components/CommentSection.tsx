"use client";

import { useState } from "react";
import Link from "next/link";

interface Comment {
  id: string;
  content: string;
  authorName: string;
  authorId: string;
  createdAt: string;
}

export default function CommentSection({
  postId,
  initialComments,
  isLoggedIn,
}: {
  postId: string;
  initialComments: Comment[];
  isLoggedIn: boolean;
}) {
  const [comments, setComments] = useState(initialComments);
  const [content, setContent] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!content.trim()) return;

    const res = await fetch(`/api/posts/${postId}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });

    if (res.ok) {
      const comment = await res.json();
      setComments([
        {
          id: comment.id,
          content: comment.content,
          authorName: comment.author.name,
          authorId: comment.author.id,
          createdAt: comment.createdAt,
        },
        ...comments,
      ]);
      setContent("");
    }
  }

  return (
    <div className="border-t border-gray-200 pt-6">
      <h2 className="text-xl font-bold text-gray-900 mb-6">
        Comments ({comments.length})
      </h2>

      {isLoggedIn ? (
        <form onSubmit={handleSubmit} className="mb-8">
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={3}
            placeholder="Write a comment..."
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 mb-2"
          />
          <button
            type="submit"
            className="bg-blue-600 text-white px-4 py-2 rounded-md text-sm hover:bg-blue-700"
          >
            Post Comment
          </button>
        </form>
      ) : (
        <p className="text-gray-500 mb-8">
          <Link href="/login" className="text-blue-600 hover:underline">
            Login
          </Link>{" "}
          to leave a comment.
        </p>
      )}

      <div className="space-y-4">
        {comments.map((comment) => (
          <div key={comment.id} className="bg-gray-50 rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2 text-sm">
              <Link
                href={`/profile/${comment.authorId}`}
                className="font-medium text-gray-900 hover:text-blue-600"
              >
                {comment.authorName}
              </Link>
              <span className="text-gray-500">
                {new Date(comment.createdAt).toLocaleDateString()}
              </span>
            </div>
            <p className="text-gray-700">{comment.content}</p>
          </div>
        ))}
        {comments.length === 0 && (
          <p className="text-gray-500 text-center py-4">
            No comments yet. Be the first!
          </p>
        )}
      </div>
    </div>
  );
}
